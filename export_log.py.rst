Reading traces as a chronological log
=====================================

UUIDs identify spans but do not describe their order. This exporter reads
the authoritative records in ``runs/`` and presents LLM calls by start time.
Requests and responses stay together, while completion times reveal calls
that overlap. Optional trace filtering isolates one graph execution.

::

  """Export locally collected traces to a readable Markdown log."""

  import argparse
  import fcntl
  import json
  import os
  import re
  import tempfile
  from datetime import datetime, timezone
  from pathlib import Path


Comparing timestamps
--------------------

Convert offsets to UTC before sorting: lexical ordering would misorder
timestamps written in different time zones. Missing or invalid timestamps
remain visible at the end, where their order is explicitly unknown.
UUIDs provide a deterministic tie-breaker, not a causal ordering.

::

  def start_time(run: dict) -> datetime | None:
      try:
          value = datetime.fromisoformat(run["start_time"].replace("Z", "+00:00"))
          return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
      except (KeyError, AttributeError, TypeError, ValueError):
          return None


Taking a consistent snapshot
----------------------------

A shared lock on the collector's lock file prevents concurrent writes while
records are loaded. Release it before formatting the log so collection can
resume promptly. Use authoritative runs rather than derived LLM files, which
may be incomplete if the collector stopped partway through a save.

::

  def load_runs(root: Path, trace_id: str | None = None, all_runs: bool = False) -> list[dict]:
      if not root.is_dir():
          raise FileNotFoundError(f"Trace directory does not exist: {root}")
      with (root / ".lock").open("a") as lock:
          fcntl.flock(lock, fcntl.LOCK_SH)
          runs = [json.loads(path.read_text(encoding="utf-8"))
                  for path in sorted((root / "runs").glob("*.json"))]
      runs = [run for run in runs
              if (all_runs or run.get("run_type") == "llm")
              and (trace_id is None or run.get("trace_id") == trace_id)]
      latest = datetime.max.replace(tzinfo=timezone.utc)
      return sorted(runs, key=lambda run: (start_time(run) or latest, str(run.get("id", ""))))


Keeping payloads readable and intact
------------------------------------

Pretty-printed JSON preserves provider-specific messages, tool calls, usage,
and Unicode text without guessing their schema. Choose a fence longer than
any backtick sequence in the payload, since prompts can contain Markdown.
Dynamic names are escaped so they cannot become headings or embedded HTML.

::

  def fenced(value: object) -> str:
      body = json.dumps(value, ensure_ascii=False, indent=2)
      longest = max((len(match) for match in re.findall(r"`+", body)), default=0)
      fence = "`" * max(3, longest + 1)
      return f"{fence}json\n{body}\n{fence}"


  def label(value: object) -> str:
      text = str(value).replace("\n", " ").replace("\r", " ")
      text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
      return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", text)


  def render_log(runs: list[dict]) -> str:
      lines = ["# LocalSmith trace log", "", f"Calls: {len(runs)}", "",
               "Ordered by start time (UTC). Calls may overlap; completion order can differ.",
               "Calls with unknown start times appear last. Equal timestamps do not establish causal order.", ""]
      if not runs:
          lines.extend(["No matching calls were collected.", ""])
      for index, run in enumerate(runs, 1):
          timestamp = start_time(run)
          started = timestamp.isoformat() if timestamp else "Unknown"
          status = "Failed" if run.get("error") else ("Completed" if run.get("end_time") else "In progress")
          lines.extend([
              f"## {index}. {label(run.get('name') or 'Unnamed call')}", "",
              f"- Type: {label(run.get('run_type', 'unknown'))}",
              f"- Started: {started}",
              f"- Ended: {label(run.get('end_time') or 'Not recorded')}",
              f"- Status: {status}",
              f"- Trace ID: {label(run.get('trace_id') or 'Not recorded')}",
              f"- Run ID: {label(run.get('id') or 'Not recorded')}",
              f"- Parent ID: {label(run.get('parent_run_id') or 'None')}", "",
              "### Request", "", fenced(run.get("inputs")), "",
              "### Response", "", fenced(run.get("outputs")), "",
          ])
          if run.get("outputs") is None:
              lines.extend(["No response recorded in this snapshot.", ""])
          if run.get("error"):
              lines.extend(["### Error", "", fenced(run["error"]), ""])
      return "\n".join(lines)


Publishing a snapshot
---------------------

Export is an explicit operation, independent of tracing ingestion. Re-running
it replaces the Markdown snapshot atomically, so an open reader never sees a
partially written report. It has no dependency on FastAPI or LangSmith.

::

  def main() -> None:
      parser = argparse.ArgumentParser(description=__doc__)
      parser.add_argument("--data-dir", type=Path,
                          default=Path(os.getenv("COLLECTOR_OUTPUT_DIR", "data")))
      parser.add_argument("--output", "-o", type=Path, default=Path("localsmith-log.md"))
      parser.add_argument("--trace-id", help="Export only one trace UUID")
      parser.add_argument("--all-runs", action="store_true", help="Include graph, chain, and tool spans")
      args = parser.parse_args()
      if args.output.suffix.lower() != ".md":
          parser.error("Output must have a .md extension")
      temporary = None
      try:
          runs = load_runs(args.data_dir, args.trace_id, args.all_runs)
          report = render_log(runs)
          args.output.parent.mkdir(parents=True, exist_ok=True)
          fd, temporary = tempfile.mkstemp(dir=args.output.parent, prefix=".export-")
          with os.fdopen(fd, "w", encoding="utf-8") as stream:
              stream.write(report)
              stream.flush()
              os.fsync(stream.fileno())
          os.replace(temporary, args.output)
      except (OSError, ValueError, TypeError, AttributeError) as error:
          parser.exit(1, f"Export failed: {error}\n")
      finally:
          if temporary is not None and os.path.exists(temporary):
              os.unlink(temporary)
      print(f"Exported {len(runs)} calls to {args.output}")


  if __name__ == "__main__":
      main()
