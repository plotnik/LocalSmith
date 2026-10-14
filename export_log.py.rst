Reading traces as a chronological log
=====================================

UUIDs identify spans but do not describe their order. This exporter reads
the authoritative records in ``runs/`` and presents LLM calls by start time.
Requests and responses stay together, while completion times reveal calls
that overlap. Optional trace filtering isolates one graph execution.

::

  """Export locally collected traces to a readable Markdown or HTML log."""

  import argparse
  import fcntl
  import json
  import os
  import re
  import tempfile
  from datetime import datetime, timezone
  from html import escape
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


Presenting message content as a dialog
--------------------------------------

Unwrap LangChain's serialized messages and nested generations to show roles
and content. Direct provider messages use role/content instead of kwargs.
JSON response content gets a formatted code block; text stays prose.
Unrecognized payloads keep a JSON fallback, and the original files always
retain metadata. Longer fences protect backticks embedded in JSON content.

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


  def messages(value: object) -> list[tuple[str, object]]:
      if isinstance(value, list):
          return [message for item in value for message in messages(item)]
      if not isinstance(value, dict):
          return []
      if isinstance(value.get("kwargs"), dict):
          return messages(value["kwargs"])
      if "content" in value:
          return [(str(value.get("role") or value.get("type") or "message"), value["content"])]
      for key in ("messages", "generations", "message"):
          if key in value:
              return messages(value[key])
      if "text" in value:
          return [("assistant", value["text"])]
      return []


  def content_text(content: object) -> str:
      if isinstance(content, str):
          return content
      if isinstance(content, list):
          return "\n\n".join(
              block if isinstance(block, str) else
              block["text"] if isinstance(block, dict) and isinstance(block.get("text"), str)
              else fenced(block)
              for block in content
          )
      return fenced(content)


  def dialog(payload: object, response: bool = False) -> str:
      extracted = messages(payload)
      if not extracted:
          return fenced(payload)
      sections = []
      for role, content in extracted:
          if response:
              if isinstance(content, str):
                  try:
                      sections.append(fenced(json.loads(content)))
                      continue
                  except ValueError:
                      pass
              sections.append(content_text(content))
          else:
              # Keep role labels inside their inline code spans while preserving
              # message content and line breaks as supplied by the application.

              safe_role = role.replace("`", "").replace("\n", " ").replace("\r", " ")
              sections.append(f"---\n`{safe_role}:` {content_text(content)}")
      return "\n\n".join(sections)


  def render_log(runs: list[dict], context_runs: list[dict] | None = None) -> str:
      runs_by_id = {run["id"]: run for run in (context_runs if context_runs is not None else runs)
                    if run.get("id")}
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
              f"- Graph node: {label(graph_node(run, runs_by_id))}",
              f"- Model: {label(llm_model(run))}",
              f"- Started: {started}",
              f"- Ended: {label(run.get('end_time') or 'Not recorded')}",
              f"- Status: {status}",
              f"- Trace ID: {label(run.get('trace_id') or 'Not recorded')}",
              f"- Run ID: {label(run.get('id') or 'Not recorded')}",
              f"- Parent ID: {label(run.get('parent_run_id') or 'None')}", "",
              "### Request", "", dialog(run.get("inputs")), "",
              "### Response", "", dialog(run.get("outputs"), response=True), "",
          ])
          if run.get("outputs") is None:
              lines.extend(["No response recorded in this snapshot.", ""])
          if run.get("error"):
              lines.extend(["### Error", "", fenced(run["error"]), ""])
      return "\n".join(lines)


Reading the dialog in a browser
------------------------------

Render the same message extraction directly as HTML, without a Markdown
dependency. Escape every traced value so prompts containing HTML remain
visible text. Inline styles make the exported document usable offline;
pre-wrapped prose preserves line breaks while JSON uses code blocks.

::

  def html_content(content: object, response: bool = False) -> str:
      if isinstance(content, str):
          if response:
              try:
                  return html_json(json.loads(content))
              except ValueError:
                  pass
          return f'<div class="content">{escape(content)}</div>'
      if isinstance(content, list):
          return "\n".join(
              html_content(block if isinstance(block, str) else block["text"], response)
              if isinstance(block, str) or (isinstance(block, dict) and isinstance(block.get("text"), str))
              else html_json(block)
              for block in content
          )
      return html_json(content)


  def html_json(value: object) -> str:
      return f'<pre><code>{escape(json.dumps(value, ensure_ascii=False, indent=2))}</code></pre>'


  def html_dialog(payload: object, response: bool = False) -> str:
      extracted = messages(payload)
      if not extracted:
          return html_json(payload)
      return "\n".join(
          '<div class="message">'
          + ("" if response else f'<code class="role">{escape(role)}:</code> ')
          + html_content(content, response) + '</div>'
          for role, content in extracted
      )


  def graph_node(run: dict, runs_by_id: dict) -> str:
      # Prefer the call's own metadata; wrappers may only record it on an
      # ancestor. Never infer a graph node from a model or chain name.
      seen = set()
      while run:
          extra = run.get("extra")
          metadata = extra.get("metadata") if isinstance(extra, dict) else None
          node = metadata.get("langgraph_node") if isinstance(metadata, dict) else None
          if isinstance(node, str) and node.strip():
              return node
          parent_id = run.get("parent_run_id")
          if not parent_id or parent_id in seen:
              break
          seen.add(parent_id)
          parent = runs_by_id.get(parent_id)
          if parent is None or parent.get("trace_id") != run.get("trace_id"):
              break
          run = parent
      return "Not recorded"


  def llm_model(run: dict) -> str:
      # Response metadata identifies the model actually used; invocation
      # parameters and tracing metadata provide fallbacks for pending calls.
      # Traverse response wrappers only, never application message content.
      def recorded_model(value: object) -> str | None:
          if isinstance(value, dict):
              for key in ("model_name", "model", "model_id", "ls_model_name"):
                  model = value.get(key)
                  if isinstance(model, str) and model.strip():
                      return model.strip()
              for key in ("response_metadata", "llm_output", "generations", "message",
                          "kwargs", "generation_info"):
                  model = recorded_model(value.get(key))
                  if model:
                      return model
          elif isinstance(value, list):
              for child in value:
                  model = recorded_model(child)
                  if model:
                      return model
          return None

      extra = run.get("extra")
      extra = extra if isinstance(extra, dict) else {}
      for source in (run.get("outputs"), extra.get("invocation_params"), extra.get("metadata")):
          model = recorded_model(source)
          if model:
              return model
      return "Not recorded"


  def token_usage(run: dict) -> tuple[int | None, int | None]:
      # Providers can repeat usage on both the run and serialized response.
      # Take the first recorded count for each direction, never sum copies.
      def candidates(value: object):
          if isinstance(value, dict):
              for key in ("usage_metadata", "token_usage", "usage"):
                  if isinstance(value.get(key), dict):
                      yield value[key]
              for key, child in value.items():
                  if key not in ("usage_metadata", "token_usage", "usage"):
                      yield from candidates(child)
          elif isinstance(value, list):
              for child in value:
                  yield from candidates(child)

      counts = [None, None]
      for source in (run.get("extra"), run.get("outputs")):
          for usage in candidates(source):
              for index, keys in enumerate((("input_tokens", "prompt_tokens"),
                                            ("output_tokens", "completion_tokens"))):
                  for key in keys:
                      value = usage.get(key)
                      if counts[index] is None and type(value) is int and value >= 0:
                          counts[index] = value
      return tuple(counts)


  def render_html(runs: list[dict], context_runs: list[dict] | None = None) -> str:
      runs_by_id = {run["id"]: run for run in (context_runs if context_runs is not None else runs)
                    if run.get("id")}
      parts = ['<!doctype html>', '<html lang="en"><head><meta charset="utf-8">',
               '<meta name="viewport" content="width=device-width, initial-scale=1">',
               '<title>LocalSmith trace log</title>', '''<style>
  body { margin: 0; background: #f4f6f8; color: #17212b; font: 16px/1.6 system-ui, sans-serif; }
  main { max-width: 1000px; margin: auto; padding: 28px 20px; }
  article { background: white; border: 1px solid #dbe2e8; border-radius: 12px; padding: 24px; margin: 24px 0; }
  h1, h2, h3 { line-height: 1.3; } h2 { margin-top: 0; }
  dl { display: grid; grid-template-columns: 90px minmax(0, 1fr); gap: 4px 12px; font-size: 14px; }
  dt { font-weight: 600; } dd { margin: 0; overflow-wrap: anywhere; }
  .message { border-top: 1px solid #dbe2e8; padding: 16px 0; }
  .role { background: #eef2f6; border-radius: 4px; padding: 2px 6px; font-weight: bold; }
  .content { white-space: pre-wrap; overflow-wrap: anywhere; }
  .role + .content { display: inline; }
  pre { background: #f4f6f8; border-radius: 6px; padding: 16px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
  code { font-family: ui-monospace, monospace; } .note { color: #536170; }
  .table-scroll { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; background: white; }
  th, td { padding: 10px 12px; border-bottom: 1px solid #dbe2e8; text-align: left; }
  thead, tfoot { background: #eef2f6; }
  .tokens { text-align: right; font-variant-numeric: tabular-nums; }
  a { color: #175ca4; } article { scroll-margin-top: 20px; }
  </style></head><body><main>''', '<h1>LocalSmith trace log</h1>',
               f'<p>Calls: {len(runs)}</p>',
               '<p class="note">Ordered by start time (UTC). Calls may overlap; completion order can differ. '
               'Calls with unknown start times appear last. Equal timestamps do not establish causal order.</p>']
      if not runs:
          parts.append('<p>No matching calls were collected.</p>')
      totals = [0, 0]
      missing = [0, 0]
      llm_calls = 0
      summary_rows = []
      for index, run in enumerate(runs, 1):
          input_tokens, output_tokens = token_usage(run)
          name = escape(str(run.get("name") or "Unnamed call"))
          node = graph_node(run, runs_by_id)
          summary_rows.append(
              f'<tr><td><a href="#step-{index}">{index}. {name}</a></td>'
              f'<td>{escape(node)}</td>'
              f'<td class="tokens">{input_tokens if input_tokens is not None else "Not recorded"}</td>'
              f'<td class="tokens">{output_tokens if output_tokens is not None else "Not recorded"}</td></tr>'
          )
          if run.get("run_type") == "llm":
              llm_calls += 1
              for direction, count in enumerate((input_tokens, output_tokens)):
                  if count is None:
                      missing[direction] += 1
                  else:
                      totals[direction] += count
          timestamp = start_time(run)
          status = "Failed" if run.get("error") else ("Completed" if run.get("end_time") else "In progress")
          parts.append(f'<article id="step-{index}"><h2>{index}. {name}</h2><dl>')
          fields = [("Type", run.get("run_type", "unknown")),
                    ("Graph node", node),
                    ("Model", llm_model(run)),
                    ("Started", timestamp.isoformat() if timestamp else "Unknown"),
                    ("Ended", run.get("end_time") or "Not recorded"), ("Status", status),
                    ("Trace ID", run.get("trace_id") or "Not recorded"),
                    ("Run ID", run.get("id") or "Not recorded"),
                    ("Parent ID", run.get("parent_run_id") or "None")]
          parts.extend(f'<dt>{name}</dt><dd>{escape(str(value))}</dd>' for name, value in fields)
          parts.extend(['</dl><h3>Request</h3>',
                        f'<p class="note">Input tokens: {input_tokens if input_tokens is not None else "Not recorded"}</p>',
                        html_dialog(run.get("inputs")), '<h3>Response</h3>',
                        f'<p class="note">Output tokens: {output_tokens if output_tokens is not None else "Not recorded"}</p>',
                        html_dialog(run.get("outputs"), response=True)])
          if run.get("outputs") is None:
              parts.append('<p class="note">No response recorded in this snapshot.</p>')
          if run.get("error"):
              parts.extend(['<h3>Error</h3>', html_json(run["error"])])
          parts.append('</article>')
      parts.extend(['<section><h2>Step summary</h2>',
                    f'<p>LLM calls: {llm_calls}</p>',
                    '<div class="table-scroll"><table aria-label="Step summary">',
                    '<thead><tr><th scope="col">Step</th><th scope="col">LangGraph node</th>'
                    '<th scope="col" class="tokens">Input tokens</th>'
                    '<th scope="col" class="tokens">Output tokens</th></tr></thead><tbody>',
                    *summary_rows, '</tbody><tfoot><tr><th scope="row" colspan="2">Total (LLM calls)</th>',
                    f'<td class="tokens">{totals[0]}</td><td class="tokens">{totals[1]}</td>',
                    '</tr></tfoot></table></div>'])
      if any(missing):
          parts.append(f'<p class="note">Totals include recorded usage only. '
                       f'Input usage missing for {missing[0]} LLM calls; '
                       f'output usage missing for {missing[1]} LLM calls.</p>')
      parts.append('</section></main></body></html>')
      return "\n".join(parts) + "\n"


Publishing a snapshot
---------------------

Export is an explicit operation, independent of tracing ingestion. Re-running
it replaces the chosen snapshot atomically, so an open reader never sees a
partially written report. It has no dependency on FastAPI or LangSmith.

::

  def main() -> None:
      parser = argparse.ArgumentParser(description=__doc__)
      parser.add_argument("--data-dir", type=Path,
                          default=Path(os.getenv("COLLECTOR_OUTPUT_DIR", "data")))
      parser.add_argument("--output", "-o", type=Path, help="Destination .md or .html file")
      parser.add_argument("--format", choices=("markdown", "html"),
                          help="Export format; otherwise inferred from the output extension")
      parser.add_argument("--trace-id", help="Export only one trace UUID")
      parser.add_argument("--all-runs", action="store_true", help="Include graph, chain, and tool spans")
      args = parser.parse_args()
      if args.output is None:
          args.output = Path("localsmith-log.html" if args.format == "html" else "localsmith-log.md")
      formats = {".md": "markdown", ".html": "html", ".htm": "html"}
      inferred = formats.get(args.output.suffix.lower())
      if inferred is None:
          parser.error("Output must have a .md, .html, or .htm extension")
      if args.format is not None and args.format != inferred:
          parser.error("Output extension must match --format")
      temporary = None
      try:
          context_runs = load_runs(args.data_dir, args.trace_id, all_runs=True)
          runs = [run for run in context_runs if args.all_runs or run.get("run_type") == "llm"]
          report = render_html(runs, context_runs) if inferred == "html" else render_log(runs, context_runs)
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
