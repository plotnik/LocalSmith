# LocalSmith

Local tracing for LangGraph and LangChain applications. LocalSmith is a small
FastAPI server that receives LangSmith SDK traces, saves LLM requests and
responses on your machine, and exports chronological dialogs to Markdown or
standalone HTML. No LangSmith cloud account is needed.

- Collect graph, chain, tool, and LLM spans through existing tracing instrumentation.
- Store each LLM call's request, response, timestamps, errors, and metadata as JSON.
- Read calls in start-time order or export a single trace.
- Run locally with a loopback endpoint and an output folder you control.

LocalSmith collects new traces after you configure your application. It does
not download historical cloud traces and is not affiliated with LangSmith.

## Start the collector

Python 3.10+ on macOS or Linux:

```bash
uv sync
uv run uvicorn collector:app --host 127.0.0.1 --port 8001
```

Or use pip from the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
uvicorn collector:app --host 127.0.0.1 --port 8001
```

`requirements.txt` installs FastAPI and Uvicorn. The exporter uses only the
Python standard library and can run directly from the repository.

To choose a destination, set `COLLECTOR_OUTPUT_DIR=/absolute/path/to/traces`
before starting the collector. The default is `./data`, relative to the
collector's working directory. Health check: `http://127.0.0.1:8001/health`.

## Configure your LangGraph or LangChain application

Set these environment variables **in your application's process**, before
importing or constructing LangSmith clients, LangChain models, or LangGraph
graphs. Restart your application:

```bash
export LANGSMITH_TRACING=true
export LANGSMITH_ENDPOINT=http://127.0.0.1:8001
export LANGSMITH_API_KEY=local
export LANGSMITH_PROJECT=localsmith-local
```

`local` is a placeholder; the collector does not authenticate it. Existing
LangChain/LangGraph tracing instrumentation sends traces to this endpoint.
If your application explicitly constructs a `langsmith.Client`, update that client's
`api_url` too. Remove conflicting legacy `LANGCHAIN_ENDPOINT` settings and
`LANGSMITH_RUNS_ENDPOINTS` multi-endpoint configuration for local-only collection.
Use the SDK's native LangSmith tracing mode rather than OTEL mode.

If your application runs in Docker on macOS, use `http://host.docker.internal:8001` and
bind the collector to an interface the container can reach (for example
`--host 0.0.0.0` on a trusted local network). If both run in Docker, use the
collector's service name on their shared network.

Custom provider calls outside LangChain need instrumentation, for example
`@traceable(run_type="llm")` or LangSmith's provider wrappers. The collector
receives whatever tracing captures; it does not intercept raw provider HTTP
traffic. Ensure your application has not enabled input/output hiding or reduced trace
sampling if you need every request and response. SDK sends run asynchronously;
flush custom clients with `client.flush()` during graceful application shutdown.

## Saved files

```text
data/
  runs/<run-uuid>.json         # merged trace spans, including chains and tools
  llm/<run-uuid>/
    request.json              # LangSmith inputs (messages/prompts)
    response.json             # LangSmith outputs (generations/usage)
    run.json                  # full span: IDs, timestamps, metadata, errors, etc.
```

The UUID is stable across start/completion updates and SDK retries. Metadata
includes the parent/trace IDs supplied by your application, so calls can be linked to their
graph execution. A response is initially `null` until a completion arrives;
failed calls preserve the SDK's `error` in `run.json`. Streaming outputs are
saved when the SDK emits its completion; this is not a live token feed.

Updates survive collector restarts and are serialized across workers. Each
file is atomically replaced; the three LLM files are not a single filesystem
transaction. `runs/<uuid>.json` is the authoritative merged record if a process
is interrupted while writing the derived files. Data remains until you delete it.
Files contain the traced prompts/responses without redaction. Keep the default
loopback binding for local use; there is no authentication layer.

## Export a readable Markdown or HTML log

Export the collected LLM calls in start-time order:

```bash
python3 export_log.py --output localsmith-log.md
```

For a standalone HTML file you can open in your browser:

```bash
python3 export_log.py --format html
# Or select the format through the output filename:
python3 export_log.py --output localsmith-log.html
```

HTML includes the same dialog and metadata, with styles embedded for offline
reading. `--format html` defaults to `localsmith-log.html`; Markdown remains the default.

The log includes each call's name, timestamps, status, trace/parent IDs,
request, response, and any error. Requests show roles and message content as a
dialog. Responses show content only, with JSON responses in formatted code
blocks. Recognized messages omit serialization and provider metadata; unknown
payloads fall back to JSON. Original files retain the full records.
It reads the authoritative
`data/runs/` records. Timestamps are compared in UTC; missing timestamps appear
last. Overlapping calls remain together with their own responses.

To isolate one execution or include graph, chain, and tool spans:

```bash
python3 export_log.py --trace-id YOUR_TRACE_UUID --output execution.md
python3 export_log.py --all-runs --output full-log.md
python3 export_log.py --data-dir /path/to/traces --output localsmith-log.md
```

`COLLECTOR_OUTPUT_DIR` also supplies the default data directory. Installing the
package with `python3 -m pip install .` (or `uv sync`) also makes
`localsmith-export` available as an equivalent command. Export is a
snapshot; run it again to include new calls or completed responses.

## Try it without an LLM API key

With the collector running in another terminal, activate the virtual environment
created above and install the development dependencies:

```bash
source .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
python3 examples/send_trace.py
python3 -m pytest
```

`requirements-dev.txt` includes the runtime dependencies, pytest, HTTPX,
the LangSmith SDK, Sphinx, and PyLit3.

Alternatively, use uv for the demo and tests:

```bash
uv sync --group dev
uv run python examples/send_trace.py
uv run pytest
```

The demo sends a synthetic LLM call through the real LangSmith SDK. For a real
end-to-end check, invoke a model in your application and inspect the
new files in `data/llm/`.

## Documentation development

Install `requirements-dev.txt` as shown above to use Sphinx and PyLit3.
Edit Python implementation in `.py` files, preserving its narrative comments.
Synchronize the paired `.py.rst` files manually when ready, then build the docs:

```bash
python3 -m pylit collector.py collector.py.rst
python3 -m pylit export_log.py export_log.py.rst
make html
```

The generated documentation is in `_build/html/`. PyLit synchronization and
Sphinx builds are separate, manual steps.

## Protocol scope

This server implements the JSON trace ingestion subset: `GET /info`,
`POST /runs`, `PATCH /runs/{id}`, and `POST /runs/batch`. Its `/info` response
tells the Python SDK to use JSON batches. It is not a full LangSmith server:
no datasets, evaluations, cloud UI, multipart attachments, OTEL ingestion, or
cloud forwarding. Pointing your application's SDK here replaces its cloud trace
destination.

## Repository contents

`collector.py` runs the receiver; `export_log.py` provides the `localsmith-export`
command. `examples/send_trace.py` demonstrates SDK tracing without a provider
API key. Python source uses PyLit comments; paired `.py.rst` files support
Sphinx documentation and are synchronized manually.

Captured traces in `data/`, generated `*-log.md` / `*-log.html` reports,
documentation builds, and virtual environments are excluded from Git. Store
custom-named exports in `exports/` to keep them excluded too.

Protocol references: [LangSmith Python client](https://github.com/langchain-ai/langsmith-sdk/blob/main/python/langsmith/client.py)
and [LangSmith API](https://api.smith.langchain.com/redoc).
