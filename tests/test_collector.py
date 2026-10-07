import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from collector import create_app


@pytest.fixture
def receiver(tmp_path):
    return TestClient(create_app(tmp_path)), tmp_path


def test_batch_completion_and_retry(receiver):
    client, root = receiver
    run_id = str(uuid4())
    start = {"id": run_id, "run_type": "llm", "inputs": {"prompt": "Привет"}}
    assert client.post("/runs/batch", json={"post": [start]}).status_code == 200
    end = {"id": run_id, "outputs": {"text": "Hello"}, "end_time": "2026-10-07T12:00:00Z"}
    assert client.post("/runs/batch", json={"patch": [end]}).status_code == 200
    client.post("/runs/batch", json={"post": [start]})
    folder = root / "llm" / run_id
    assert json.loads((folder / "request.json").read_text()) == start["inputs"]
    assert json.loads((folder / "response.json").read_text()) == end["outputs"]
    assert json.loads((folder / "run.json").read_text())["end_time"] == end["end_time"]


def test_patch_before_create_and_restart(receiver):
    client, root = receiver
    run_id = str(uuid4())
    client.patch(f"/runs/{run_id}", json={"outputs": {"answer": 42}, "error": "example"})
    client = TestClient(create_app(root))
    client.post("/runs", json={"id": run_id, "run_type": "llm", "inputs": {}})
    saved = json.loads((root / "llm" / run_id / "run.json").read_text())
    assert saved["outputs"] == {"answer": 42}
    assert saved["error"] == "example"


def test_parent_and_validation(receiver):
    client, root = receiver
    run_id = str(uuid4())
    assert client.post("/runs", json={"id": run_id, "run_type": "chain"}).status_code == 201
    assert (root / "runs" / f"{run_id}.json").exists()
    assert not (root / "llm").exists()
    assert client.post("/runs", json={"id": "../escape"}).status_code == 422
    assert client.post("/runs/batch", json={"post": [{"id": run_id}, {"id": "bad"}]}).status_code == 422
    assert client.patch(f"/runs/{run_id}", json={"id": str(uuid4())}).status_code == 422
    assert client.get("/info").json()["batch_ingest_config"]["use_multipart_endpoint"] is False


def test_concurrent_updates(receiver):
    from concurrent.futures import ThreadPoolExecutor
    client, root = receiver
    run_id = str(uuid4())
    client.post("/runs", json={"id": run_id, "run_type": "llm"})
    def update(index):
        return client.patch(f"/runs/{run_id}", json={f"field_{index}": index}).status_code
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(update, range(20))) == [200] * 20
    saved = json.loads((root / "runs" / f"{run_id}.json").read_text())
    assert all(saved[f"field_{i}"] == i for i in range(20))


def test_real_langsmith_sdk(receiver):
    """Exercise SDK discovery, serialization, and its background batch sender."""
    import requests
    from langsmith import Client, traceable, tracing_context

    receiver_client, root = receiver

    class LocalSession(requests.Session):
        def request(self, method, url, **kwargs):
            from urllib.parse import urlsplit
            response = receiver_client.request(
                method, urlsplit(url).path,
                content=kwargs.get("data"), json=kwargs.get("json"),
                headers=kwargs.get("headers"), params=kwargs.get("params"),
            )
            result = requests.Response()
            result.status_code = response.status_code
            result._content = response.content
            result.headers.update(response.headers)
            result.url = url
            return result

    sdk = Client(api_url="http://localhost:8001", api_key="local", session=LocalSession())

    @traceable(run_type="llm", client=sdk, project_name="localsmith-test")
    def model(prompt):
        return {"answer": "Hello"}

    try:
        with tracing_context(enabled=True):
            model("Hi")
        sdk.flush(timeout=5)
        folders = list((root / "llm").iterdir())
        assert len(folders) == 1
        assert json.loads((folders[0] / "request.json").read_text()) == {"prompt": "Hi"}
        assert json.loads((folders[0] / "response.json").read_text()) == {"answer": "Hello"}
    finally:
        sdk.session.close()
