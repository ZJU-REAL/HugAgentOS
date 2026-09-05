from __future__ import annotations

import asyncio

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.v1 import desktop_capability as routes
from core.services import desktop_capability as service


def test_model_gateway_replaces_model_and_credentials(monkeypatch):
    captured: dict = {}

    class EventStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[]}\n\n'

    def resolve_target(user_id: str, provider_id: str):
        assert user_id == "user-1"
        assert provider_id == "provider-1"
        return {
            "url": "http://192.0.2.10:1029/v1/chat/completions",
            "api_key": "real-cloud-key",
            "model_name": "deepseek-private",
            "provider_type": "chat",
            "path": "chat/completions",
        }

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["json"] = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "x-secret-upstream": "drop-me"},
            stream=EventStream(),
        )

    monkeypatch.setattr(service, "resolve_model_gateway_target", resolve_target)
    gateway_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    monkeypatch.setattr(routes, "_gateway_client", gateway_client)

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_capability_user] = lambda: "user-1"
    with TestClient(app) as client:
        response = client.post(
            "/v1/desktop/capability/gateway/models/provider-1/chat/completions",
            headers={
                "authorization": "Bearer desktop-capability-token",
                "x-api-key": "forged-key",
                "cookie": "forged=1",
            },
            json={"model": "attacker-selected-model", "messages": [], "stream": True},
        )

    asyncio.run(gateway_client.aclose())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "x-secret-upstream" not in response.headers
    assert captured["url"] == "http://192.0.2.10:1029/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer real-cloud-key"
    assert "x-api-key" not in captured["headers"]
    assert "cookie" not in captured["headers"]
    assert captured["json"]["model"] == "deepseek-private"


def test_model_gateway_rejects_wrong_protocol_path(monkeypatch):
    monkeypatch.setattr(
        service,
        "resolve_model_gateway_target",
        lambda _user_id, _provider_id: {
            "url": "https://models.example/v1/chat/completions",
            "api_key": "real-key",
            "model_name": "chat-model",
            "provider_type": "chat",
            "path": "chat/completions",
        },
    )
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_capability_user] = lambda: "user-1"
    with TestClient(app) as client:
        response = client.post(
            "/v1/desktop/capability/gateway/models/provider-1/embeddings",
            json={"model": "chat-model", "input": "secret"},
        )
    assert response.status_code == 404


def test_manifest_endpoint_supports_revision_revalidation(monkeypatch):
    manifest = {"version": 2, "revision": "a" * 64, "servers": []}
    monkeypatch.setattr(service, "build_user_capability_manifest", lambda _uid: manifest)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_capability_user] = lambda: "user-1"

    with TestClient(app) as client:
        first = client.get("/v1/desktop/capability/manifest")
        unchanged = client.get(
            "/v1/desktop/capability/manifest",
            headers={"if-none-match": '"' + "a" * 64 + '"'},
        )

    assert first.status_code == 200
    assert first.headers["etag"] == '"' + "a" * 64 + '"'
    assert unchanged.status_code == 304


def test_mcp_gateway_disables_upstream_compression(monkeypatch):
    captured: dict = {}

    class JsonStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"jsonrpc":"2.0","id":1,"result":{}}'

    monkeypatch.setattr(
        service,
        "resolve_gateway_target",
        lambda user_id, server_id: (
            {
                "url": f"https://mcp.example/{server_id}",
                "headers": {"Authorization": "Bearer gateway-upstream-key"},
            }
            if user_id == "user-1"
            else None
        ),
    )

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "mcp-session-id": "session-1"},
            stream=JsonStream(),
        )

    gateway_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    monkeypatch.setattr(routes, "_gateway_client", gateway_client)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_capability_user] = lambda: "user-1"

    with TestClient(app) as client:
        response = client.post(
            "/v1/desktop/capability/gateway/test-server/mcp",
            headers={"accept-encoding": "gzip, deflate", "content-type": "application/json"},
            content=b'{"jsonrpc":"2.0","id":1,"method":"initialize"}',
        )

    asyncio.run(gateway_client.aclose())
    assert response.status_code == 200
    assert captured["headers"]["accept-encoding"] == "identity"
    assert captured["headers"]["authorization"] == "Bearer gateway-upstream-key"


def test_mcp_json_call_gateway_uses_schema_revision_and_safe_context(monkeypatch):
    captured: dict = {}

    def resolve(user_id, server_id, tool_name, *, schema_hash):
        captured["schema_hash"] = schema_hash
        if (user_id, server_id, tool_name) != ("user-1", "server-1", "search"):
            return None
        return {
            "user_id": user_id,
            "server_id": server_id,
            "target": {"url": "https://mcp.example/mcp"},
            "tool": {
                "name": tool_name,
                "description": "Search",
                "inputSchema": {"type": "object", "properties": {}},
            },
        }

    async def invoke(resolved, arguments, runtime_headers):
        captured["resolved"] = resolved
        captured["arguments"] = arguments
        captured["runtime_headers"] = runtime_headers
        return {
            "content": [{"type": "text", "text": "ok"}],
            "state": "running",
            "is_last": True,
            "metadata": {"origin": "cloud"},
            "id": "chunk-1",
        }

    monkeypatch.setattr(service, "resolve_gateway_tool", resolve)
    monkeypatch.setattr(service, "invoke_gateway_tool", invoke)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_capability_user] = lambda: "user-1"

    with TestClient(app) as client:
        response = client.post(
            "/v1/desktop/capability/gateway/server-1/call",
            headers={
                "x-current-user-id": "forged-user",
                "x-chat-id": "chat-1",
                "x-reranker-enabled": "true",
                "x-api-key": "must-not-reach-upstream",
            },
            json={
                "tool_name": "search",
                "arguments": {"query": "hello"},
                "schema_hash": "a" * 64,
            },
        )

    assert response.status_code == 200
    assert captured["schema_hash"] == "a" * 64
    assert captured["arguments"] == {"query": "hello"}
    assert captured["runtime_headers"] == {
        "x-chat-id": "chat-1",
        "x-reranker-enabled": "true",
    }
