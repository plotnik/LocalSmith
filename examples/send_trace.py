"""Exercise tracing without an LLM provider or API key."""
from langsmith import Client, traceable

client = Client(api_url="http://127.0.0.1:8001", api_key="local")


@traceable(run_type="llm", name="demo-llm", client=client, project_name="localsmith-local")
def demo(messages: list[dict]) -> dict:
    return {"message": {"role": "assistant", "content": "Hello from LocalSmith!"}}


if __name__ == "__main__":
    from langsmith import tracing_context
    with tracing_context(enabled=True):
        demo([{"role": "user", "content": "Hello"}])
    client.flush()
    print("Trace sent. Check data/llm/ for request.json and response.json.")
