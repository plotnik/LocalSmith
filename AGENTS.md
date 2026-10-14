# Repository Guidelines

## Project Structure & Module Organization

LocalSmith is a Python 3.10+ local trace receiver for macOS and Linux. `collector.py` contains the FastAPI application and JSON persistence; `export_log.py` provides Markdown/HTML rendering and the `localsmith-export` CLI. Tests live in `tests/test_collector.py` and `tests/test_export_log.py`. `examples/send_trace.py` sends a synthetic trace through the LangSmith SDK. Sphinx documentation uses `index.rst`, `conf.py`, paired `.py.rst` files, `_static/`, and `_templates/`.

## Build, Test, and Development Commands

- `uv sync --group dev`: install runtime and development dependencies.
- `uv run uvicorn collector:app --host 127.0.0.1 --port 8001`: start the local collector.
- `uv run pytest`: run the full test suite.
- `uv run pytest tests/test_export_log.py`: run exporter tests only.
- `uv run python examples/send_trace.py`: exercise SDK tracing against a running collector without an LLM API key.
- `uv run python export_log.py --output localsmith-log.html`: export collected calls as standalone HTML.

`make html` builds Sphinx documentation when Sphinx is installed separately; documentation builds are manual.

## Coding Style & Naming Conventions

Use four-space indentation, snake_case functions and variables, PascalCase classes, and uppercase constants. Follow existing import grouping and type annotations. Preserve PyLit-compatible narrative comments and code organization. Edit implementation in `.py` files; leave paired `.py.rst` synchronization and Sphinx builds to the user unless explicitly requested. No formatter or linter is configured in `pyproject.toml`.

## Testing Guidelines

Use pytest, `test_*.py` filenames, and descriptive `test_*` functions. Isolate persistence with `tmp_path` and exercise endpoints with FastAPI `TestClient`. Add regression tests for affected behavior, especially retries, concurrent updates, restart persistence, export ordering, metadata, and HTML escaping. Use synthetic payloads; no provider credentials are needed. No minimum coverage threshold is configured.

## Commit & Pull Request Guidelines

History uses brief descriptive messages such as `tokens` and `langgraph node in reports`, without a formal prefix convention. Write concise, action-oriented summaries. PRs should explain the behavior change, relevant issues, and validation commands/results. Include sample output or screenshots for report presentation changes and identify changed Python files whose paired documentation needs updating.

## Security & Configuration

Keep the collector bound to loopback: it has no authentication. Set `COLLECTOR_OUTPUT_DIR` to choose trace storage; the default is `data/`. Traces contain unredacted prompts and responses. Keep credentials, `.env`, captured traces, and generated reports out of commits; place custom-named exports in ignored `exports/`.
