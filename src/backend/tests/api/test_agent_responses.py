"""Public JSON/SSE contract for the unified agent response endpoint."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.schemas import ChatRequest
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db


@pytest.fixture
def response_client(monkeypatch):
    from api.routes.v1 import agent_responses as responses
    from api.routes.v1 import chats

    app = FastAPI()
    app.include_router(responses.router)
    app.include_router(chats.router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="user-1", user_center_id="center-1", username="test"
    )
    db = SimpleNamespace(commit=lambda: None, rollback=lambda: None, close=lambda: None)
    app.dependency_overrides[get_db] = lambda: db

    seen = {}
    async def start(request, user, session):
        seen["stream"] = request.stream
        return SimpleNamespace(run_id="run-1", message_id="message-1", chat_id=request.chat_id)
    async def follow(run_id, *, chat_id):
        yield 'event: run_started\ndata: {"run_id":"run-1"}\n\n'
        yield 'data: [DONE]\n\n'
    async def wait(run_id, *, chat_id, message_id):
        from api.schemas import ChatResponse
        return ChatResponse(chat_id=chat_id, response="hello", timestamp="2026-09-26")

    monkeypatch.setattr(responses, "_start_response_run", start)
    monkeypatch.setattr(responses.chat_run_executor, "follow_run_as_sse", follow)
    monkeypatch.setattr(responses, "_wait_response", wait)
    return TestClient(app), seen, app


@pytest.mark.parametrize("stream", [False, True])
def test_one_endpoint_selects_response_transport(response_client, stream):
    client, seen, _app = response_client
    result = client.post("/v1/agents/responses", json={
        "chat_id": "chat-1", "message": "hello", "stream": stream,
    })
    assert result.status_code == 200
    assert seen == {"stream": stream}
    if stream:
        assert result.headers["content-type"].startswith("text/event-stream")
        assert "run_started" in result.text
        assert result.text.endswith("data: [DONE]\n\n")
    else:
        assert result.headers["content-type"].startswith("application/json")
        assert result.json()["response"] == "hello"
        assert "data" not in result.json()


def test_transport_defaults_to_json(response_client):
    client, seen, _app = response_client
    result = client.post("/v1/agents/responses", json={"chat_id": "chat-1", "message": "hello"})
    assert result.json()["response"] == "hello"
    assert seen["stream"] is False


def test_openapi_declares_both_transports_and_old_posts_are_removed(response_client):
    client, _seen, app = response_client
    schema = app.openapi()
    content = schema["paths"]["/v1/agents/responses"]["post"]["responses"]["200"]["content"]
    assert "application/json" in content
    assert "text/event-stream" in content
    assert "post" not in schema["paths"].get("/v1/chats/send", {})
    assert "post" not in schema["paths"].get("/v1/chats/stream", {})
    assert "get" in schema["paths"]["/v1/chats/stream/{run_id}"]
    for path in ("/v1/chats/send", "/v1/chats/stream"):
        assert client.post(path, json={"chat_id": "chat-1", "message": "hello"}).status_code in (404, 405)


@pytest.mark.parametrize("value", ["true", 1, None, [], {}])
def test_stream_requires_a_json_boolean(value):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ChatRequest(chat_id="chat-1", message="hello", stream=value)


@pytest.mark.parametrize("status", [200, 409, 500])
def test_scoped_json_logs_actual_http_result(response_client, monkeypatch, status):
    from api.routes.v1 import agent_responses
    from core.services import agent_api_service
    from fastapi import HTTPException

    client, _seen, app = response_client
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="user-1", user_center_id="center-1", username="test",
        api_key_id="key-1", api_key_agent_id="agent-1",
    )
    recorded = []
    monkeypatch.setattr(
        agent_api_service, "record_agent_api_http_status",
        lambda run_id, code: recorded.append((run_id, code)),
    )
    if status != 200:
        async def failed_wait(*args, **kwargs):
            raise HTTPException(status_code=status, detail="terminal result")
        monkeypatch.setattr(agent_responses, "_wait_response", failed_wait)

    result = client.post("/v1/agents/responses", json={
        "chat_id": "chat-1", "message": "hello", "stream": False,
    })
    assert result.status_code == status
    assert recorded == [("run-1", status)]
