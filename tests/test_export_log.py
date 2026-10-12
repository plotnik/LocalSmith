import json

import pytest

from export_log import load_runs, main, render_html, render_log, token_usage


def test_serialized_dialog_and_json_response():
    report = render_log([{
        "id": "example", "inputs": {"messages": [[
            {"lc": 1, "type": "constructor", "kwargs": {"type": "system", "content": "Resolve this turn."}},
            {"kwargs": {"type": "human", "content": "Respond exactly."}},
        ]]},
        "outputs": {"generations": [[{
            "text": "duplicate", "generation_info": {"model_name": "hidden"},
            "message": {"kwargs": {"type": "ai", "content": '{"answer":"Привет"}',
                                    "usage_metadata": {"tokens": 100}}},
        }]]},
    }])
    assert "`system:` Resolve this turn.\n\n---\n`human:` Respond exactly." in report
    assert '### Response\n\n```json\n{\n  "answer": "Привет"\n}\n```' in report
    assert all(text not in report for text in ("kwargs", "generation_info", "usage_metadata", "duplicate"))


def test_direct_messages_and_content_blocks():
    report = render_log([{
        "inputs": {"messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}]},
        "outputs": {"message": {"role": "assistant", "content": "Plain reply\nsecond line"}},
    }])
    assert "`user:` Hello" in report
    assert "### Response\n\nPlain reply\nsecond line" in report


def test_chronology_filter_and_missing_timestamp(tmp_path):
    folder = tmp_path / "runs"
    folder.mkdir()
    records = [
        {"id": "later", "run_type": "llm", "trace_id": "a", "start_time": "2026-10-07T10:00:00Z"},
        {"id": "earlier", "run_type": "llm", "trace_id": "a", "start_time": "2026-10-07T11:00:00+02:00"},
        {"id": "unknown", "run_type": "llm", "trace_id": "a"},
        {"id": "chain", "run_type": "chain", "trace_id": "a", "start_time": "2026-10-07T08:00:00Z"},
        {"id": "other", "run_type": "llm", "trace_id": "b"},
    ]
    for record in records:
        (folder / f"{record['id']}.json").write_text(json.dumps(record))
    assert [run["id"] for run in load_runs(tmp_path, "a")] == ["earlier", "later", "unknown"]
    assert load_runs(tmp_path, "a", all_runs=True)[0]["id"] == "chain"


def test_payload_fences_errors_and_pending():
    report = render_log([
        {"id": "1", "name": "<model>*", "inputs": {"prompt": "Привет ```"}, "error": "Failed"},
        {"id": "2", "inputs": {}, "outputs": None},
    ])
    assert "&lt;model&gt;\\*" in report
    assert '````json\n{\n  "prompt": "Привет ```"\n}\n````' in report
    assert "Status: Failed" in report
    assert "### Error" in report
    assert "Status: In progress" in report
    assert "No response recorded" in report


def test_cli_export(tmp_path, monkeypatch):
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "example.json").write_text(json.dumps({
        "id": "example", "name": "Demo", "run_type": "llm",
        "inputs": {"prompt": "Hello"}, "outputs": {"text": "Hi"},
        "start_time": "2026-10-07T10:00:00Z", "end_time": "2026-10-07T10:00:01Z",
    }))
    output = tmp_path / "logs" / "export.md"
    monkeypatch.setattr("sys.argv", ["export_log.py", "--data-dir", str(tmp_path), "-o", str(output)])
    main()
    report = output.read_text()
    assert "## 1. Demo" in report
    assert "Status: Completed" in report
    assert '"prompt": "Hello"' in report
    assert "### Response\n\nHi" in report


def test_html_dialog_escaping_and_json():
    report = render_html([{
        "name": "<script>alert(1)</script>", "id": "example",
        "inputs": {"messages": [[
            {"kwargs": {"type": "system", "content": "First line\nSecond line <b>literal</b>"}},
            {"role": "human", "content": [{"type": "text", "text": "Привет"}]},
        ]]},
        "outputs": {"generations": [[{"message": {"kwargs": {
            "content": '{"answer":"<script>literal</script>"}', "usage_metadata": {"tokens": 10},
        }}}]]},
    }])
    assert report.startswith("<!doctype html>")
    assert '<code class="role">system:</code>' in report
    assert 'First line\nSecond line &lt;b&gt;literal&lt;/b&gt;' in report
    assert 'Привет' in report
    assert '<pre><code>{\n  &quot;answer&quot;:' in report
    assert '<script>' not in report
    assert 'usage_metadata' not in report
    assert '<h3>Response</h3>' in report


@pytest.mark.parametrize("arguments,filename", [
    (["--format", "html"], "localsmith-log.html"),
    (["--output", "custom.html"], "custom.html"),
    (["--output", "custom.htm"], "custom.htm"),
    ([], "localsmith-log.md"),
])
def test_cli_format_selection(tmp_path, monkeypatch, arguments, filename):
    (tmp_path / "data" / "runs").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["export_log.py", *arguments])
    main()
    report = (tmp_path / filename).read_text()
    assert "No matching calls" in report
    assert report.startswith("# LocalSmith" if filename.endswith(".md") else "<!doctype html>")


def test_cli_rejects_conflicting_format(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["export_log.py", "--format", "html", "-o", "log.md"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert not (tmp_path / "log.md").exists()


def test_html_errors_and_pending():
    report = render_html([{"error": "<failed>", "outputs": None}, {}])
    assert '<dd>Failed</dd>' in report
    assert '<dd>In progress</dd>' in report
    assert '<h3>Error</h3>' in report
    assert '&lt;failed&gt;' in report
    assert 'No response recorded' in report


@pytest.mark.parametrize("renderer", [render_html, render_log])
def test_graph_node_metadata_and_missing_context(renderer):
    report = renderer([
        {"extra": {"metadata": {"langgraph_node": "<resolve_context>"}}},
        {"name": "ChatModel", "extra": None},
        {"id": "cycle", "parent_run_id": "cycle"},
    ])
    if renderer is render_html:
        assert '<dt>Graph node</dt><dd>&lt;resolve_context&gt;</dd>' in report
        assert report.count('<dt>Graph node</dt><dd>Not recorded</dd>') == 2
    else:
        assert r'- Graph node: &lt;resolve\_context&gt;' in report
        assert report.count('- Graph node: Not recorded') == 2


@pytest.mark.parametrize("extension", ["html", "md"])
def test_export_inherits_node_from_hidden_ancestors(tmp_path, monkeypatch, extension):
    folder = tmp_path / "runs"
    folder.mkdir()
    records = [
        {"id": "node", "run_type": "chain", "trace_id": "a",
         "extra": {"metadata": {"langgraph_node": "run_sql"}}},
        {"id": "wrapper", "run_type": "chain", "trace_id": "a", "parent_run_id": "node"},
        {"id": "llm", "run_type": "llm", "trace_id": "a", "parent_run_id": "wrapper"},
        {"id": "own", "run_type": "llm", "trace_id": "a", "parent_run_id": "node",
         "extra": {"metadata": {"langgraph_node": "inner_node"}}},
        {"id": "other", "run_type": "llm", "trace_id": "b", "parent_run_id": "node"},
    ]
    for record in records:
        (folder / f"{record['id']}.json").write_text(json.dumps(record))
    output = tmp_path / f"report.{extension}"
    monkeypatch.setattr("sys.argv", ["export_log.py", "--data-dir", str(tmp_path), "-o", str(output)])
    main()
    report = output.read_text()
    if extension == "html":
        assert '<p>Calls: 3</p>' in report
        assert report.count('<article id=') == 3
        assert '<dt>Graph node</dt><dd>run_sql</dd>' in report
        assert '<dt>Graph node</dt><dd>inner_node</dd>' in report
        assert '<dt>Graph node</dt><dd>Not recorded</dd>' in report
    else:
        assert 'Calls: 3' in report
        assert sum(line.startswith('## ') for line in report.splitlines()) == 3
        assert r'- Graph node: run\_sql' in report
        assert r'- Graph node: inner\_node' in report
        assert '- Graph node: Not recorded' in report


@pytest.mark.parametrize("outputs,expected", [
    ({"generations": [[{"message": {"kwargs": {"usage_metadata": {
        "input_tokens": 12, "output_tokens": 3}}}}]]}, (12, 3)),
    ({"llm_output": {"token_usage": {"prompt_tokens": 8, "completion_tokens": 0}}}, (8, 0)),
    ({"usage": {"input_tokens": 0, "output_tokens": 4}}, (0, 4)),
    ({"usage_metadata": {"input_tokens": -1, "output_tokens": True}}, (None, None)),
    (None, (None, None)),
])
def test_token_usage_response_formats(outputs, expected):
    assert token_usage({"outputs": outputs}) == expected


def test_html_token_counts_totals_and_duplicate_usage():
    usage = {"input_tokens": 12, "output_tokens": 3}
    report = render_html([
        {"run_type": "llm", "extra": {"metadata": {"usage_metadata": usage}},
         "outputs": {"message": {"usage_metadata": usage, "content": "Hello"}}},
        {"run_type": "llm", "outputs": {"llm_output": {
            "token_usage": {"prompt_tokens": 8, "completion_tokens": 0}}}},
        {"run_type": "llm", "extra": {"metadata": {"usage_metadata": {"input_tokens": 5}}}},
        {"run_type": "chain", "extra": {"metadata": {"usage_metadata": usage}}},
    ])
    assert '<h3>Request</h3>\n<p class="note">Input tokens: 12</p>' in report
    assert '<h3>Response</h3>\n<p class="note">Output tokens: 3</p>' in report
    assert 'Output tokens: 0</p>' in report
    assert 'Output tokens: Not recorded</p>' in report
    summary = report.split('<h2>Step summary</h2>')[1]
    assert '<p>LLM calls: 3</p>' in summary
    assert '<td class="tokens">25</td><td class="tokens">3</td>' in summary
    assert 'Input usage missing for 0 LLM calls; output usage missing for 1 LLM calls.' in summary


def test_html_empty_token_totals():
    report = render_html([])
    assert '<td class="tokens">0</td><td class="tokens">0</td>' in report


def test_html_step_summary_links_and_escaping():
    runs = [
        {"name": '<model>"', "run_type": "llm", "extra": {"metadata": {
            "langgraph_node": "<node>", "usage_metadata": {"input_tokens": 10, "output_tokens": 2}}}},
        {"name": '<model>"', "run_type": "llm"},
    ]
    report = render_html(runs)
    summary = report.split('<h2>Step summary</h2>')[1]
    for index in (1, 2):
        assert report.count(f'id="step-{index}"') == 1
        assert f'<a href="#step-{index}">{index}. &lt;model&gt;&quot;</a>' in summary
    assert '<td>&lt;node&gt;</td><td class="tokens">10</td><td class="tokens">2</td>' in summary
    assert '<td>Not recorded</td><td class="tokens">Not recorded</td>' in summary
    assert summary.index('href="#step-1"') < summary.index('href="#step-2"')
    assert report.index('<h2>Step summary</h2>') > report.rindex('</article>')
    assert '<tfoot>' in summary
