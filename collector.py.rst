LocalSmith local trace collector
===============================

LangGraph and LangChain applications describe executions through LangSmith
tracing. This service receives that traffic and turns it into local JSON
documents. A run represents one span: a graph, chain, tool, or LLM call.
Starts and completions can arrive separately, so storage must reconstruct
each run before exposing its request and response.

The implementation follows the data from validation, through persistence,
to the HTTP endpoints used by the SDK. It implements JSON trace ingestion;
it does not provide the rest of the LangSmith platform.

::

  """Receive the JSON tracing subset of the LangSmith API and store it locally."""

  import fcntl
  import json
  import os
  import tempfile
  from pathlib import Path
  from uuid import UUID

  from fastapi import FastAPI, HTTPException
  from pydantic import BaseModel, ConfigDict, Field


Accepting trace records
-----------------------

Only the run identifier is mandatory here. Completion patches often omit
the name, type, and inputs supplied at creation. Allowing extra fields
preserves provider metadata and future SDK fields without maintaining a
local copy of the full LangSmith schema.

Parsing the identifier as a UUID also gives storage a canonical, safe file
name: client-supplied identifiers cannot become arbitrary filesystem paths.

::

  class Run(BaseModel):
      model_config = ConfigDict(extra="allow")
      id: UUID


A batch contains creations under ``post`` and updates under ``patch``.
Unlike the flexible run record, this envelope rejects unknown fields so
an unsupported operation cannot be silently acknowledged and discarded.
FastAPI validates the entire envelope before the handler writes any runs.

::

  class Batch(BaseModel):
      model_config = ConfigDict(extra="forbid")
      post: list[Run] = Field(default_factory=list)
      patch: list[Run] = Field(default_factory=list)


Publishing complete JSON files
------------------------------

A reader may inspect a trace while a completion is being saved. Writing to
a temporary file in the destination directory and replacing the destination
lets readers see either the previous document or the complete new one.
Keeping both files on the same filesystem makes that replacement atomic.

Flush and fsync the temporary file before replacement. The final cleanup
removes it if serialization or replacement fails. This protects individual
files; it does not make a group of related files one transaction or guarantee
directory-entry durability across power loss.

::

  def write_json(path: Path, value: object) -> None:
      """Atomically replace a file so readers never see partial JSON."""
      path.parent.mkdir(parents=True, exist_ok=True)
      fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
      try:
          with os.fdopen(fd, "w", encoding="utf-8") as stream:
              json.dump(value, stream, ensure_ascii=False, indent=2)
              stream.write("\n")
              stream.flush()
              os.fsync(stream.fileno())
          os.replace(temporary, path)
      finally:
          if os.path.exists(temporary):
              os.unlink(temporary)


Reconstructing a run on disk
----------------------------

The authoritative record is ``runs/<uuid>.json``. Reading it for each update
makes reconstruction independent of process memory and preserves progress
across server restarts. LLM spans additionally produce convenient request,
response, and full-run documents under ``llm/<uuid>/``.

A single filesystem lock serializes writers across threads and worker
processes. The lock covers both the merge and derived-file writes, preventing
two writers from reading the same old record and losing each other's fields.
This uses Unix ``flock`` and therefore targets macOS and Linux.

::

  class Store:
      def __init__(self, root: Path):
          self.root = root

      def save(self, run: Run, operation: str) -> None:
          # Preserve the difference between an omitted field and explicit null.
          # A partial patch must not clear values merely because it omits them.

          incoming = run.model_dump(mode="json", exclude_unset=True)
          run_id = str(run.id)
          self.root.mkdir(parents=True, exist_ok=True)
          with (self.root / ".lock").open("a") as lock:
              fcntl.flock(lock, fcntl.LOCK_EX)
              path = self.root / "runs" / f"{run_id}.json"
              existing = json.loads(path.read_text()) if path.exists() else {}
              # Creations fill missing fields; patches replace supplied fields.
              # This allows a completion to arrive before its creation, and a
              # retried creation cannot erase an already received response.
              # The merge is shallow: a supplied nested object replaces that
              # field as a whole. Patches take precedence in arrival order.

              merged = ({**incoming, **existing} if operation == "post"
                        else {**existing, **incoming})
              write_json(path, merged)

              # Consult the merged type because a completion may omit it.
              # Before completion, response.json contains null. Failed runs
              # retain their error in the full record even without outputs.
              # These derived files are replaced individually; the authoritative
              # record above remains available if this sequence is interrupted.

              if merged.get("run_type") == "llm":
                  folder = self.root / "llm" / run_id
                  write_json(folder / "request.json", merged.get("inputs"))
                  write_json(folder / "response.json", merged.get("outputs"))
                  write_json(folder / "run.json", merged)


Connecting the SDK to storage
-----------------------------

The application factory accepts an explicit directory for tests and embedded
use. Otherwise, the environment selects the output directory, with ``data``
relative to the server's working directory as the default. Constructing the
application does not create files; storage starts with the first trace.

::

  def create_app(output_dir: str | Path | None = None) -> FastAPI:
      app = FastAPI(title="LocalSmith local trace collector")
      store = Store(Path(output_dir or os.getenv("COLLECTOR_OUTPUT_DIR", "data")))

      @app.get("/health")
      def health():
          return {"status": "ok"}

      # The SDK discovers ingestion capabilities through /info. Advertising
      # JSON batches keeps traffic within the protocol this collector supports.
      # The size and worker settings configure the SDK's background sender;
      # they are not server-side request limits. The version is a compatibility
      # value, not a claim that this is a complete LangSmith installation.

      @app.get("/info")
      def info():
          return {"version": "0.0.1", "batch_ingest_config": {
              "use_multipart_endpoint": False, "size_limit_bytes": 20_000_000,
              "size_limit": 100, "scale_up_nthreads_limit": 1,
              "scale_up_qsize_trigger": 1000, "scale_down_nempty_trigger": 4,
          }}

      # Within a batch, apply starts before completions so the final record
      # includes both. Acknowledgment follows successful writes. A batch is not
      # transactional: if a later write fails, earlier records remain saved and
      # the SDK can retry using the same run identifiers.

      @app.post("/runs/batch")
      def batch(payload: Batch):
          for run in payload.post:
              store.save(run, "post")
          for run in payload.patch:
              store.save(run, "patch")
          return {"ok": True}

      # Reject unsupported multipart traffic explicitly. A client that cached
      # capabilities from another endpoint must rediscover this server's /info.

      @app.post("/runs/multipart")
      def multipart():
          raise HTTPException(415, "Multipart is unsupported. Use a fresh LangSmith "
                              "client so it reads /info and selects JSON batches.")

      # The single-run endpoints share the same merge rules as batch ingestion.
      # For a patch, the URL supplies the identifier; an optional body identifier
      # must agree with it before any data is written.

      @app.post("/runs", status_code=201)
      def create_run(payload: Run):
          store.save(payload, "post")
          return {"id": str(payload.id)}

      @app.patch("/runs/{run_id}")
      def update_run(run_id: UUID, payload: dict):
          if "id" in payload and str(payload["id"]) != str(run_id):
              raise HTTPException(422, "Body ID must match URL ID")
          store.save(Run.model_validate({**payload, "id": run_id}), "patch")
          return {"id": str(run_id)}

      return app


Uvicorn loads this default application through ``collector:app``. Tests can
instead call the factory with a temporary directory to isolate their data.

::

  app = create_app()
