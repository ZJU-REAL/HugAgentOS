from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.v1 import desktop_capability as routes
from core.services import desktop_capability as service


@pytest.fixture(autouse=True)
def _fake_connection_secrets(monkeypatch):
    monkeypatch.setattr(service, "_known_cloud_secrets", lambda uid, **_: set())


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


def _provider(provider_type: str, provider: str, api_protocol):
    from types import SimpleNamespace

    return SimpleNamespace(
        provider_type=provider_type,
        provider=provider,
        extra_config={"api_protocol": api_protocol} if api_protocol else {},
    )


def test_gateway_forwards_each_model_on_the_protocol_it_actually_speaks():
    """A Responses-protocol model must not be proxied to ``/chat/completions``.

    The path used to come from the provider *type* alone, so every model whose
    probe found ``/responses`` was forwarded to ``/chat/completions`` and came
    back 404. On the desktop that is not a visible error: failover quietly walks
    on to whichever model does speak chat completions, so every local turn paid
    a wasted upstream round trip and then ran on a model nobody selected.
    """
    resolve = service._upstream_model_path

    assert resolve(_provider("chat", "openai_compatible", "responses")) == "responses"
    assert resolve(_provider("chat", "deepseek", "responses")) == "responses"
    assert (
        resolve(_provider("chat", "openai_compatible", "chat_completions")) == "chat/completions"
    )
    assert resolve(_provider("chat", "zhipu", "chat_completions")) == "chat/completions"
    # Never probed: same default the model client itself applies.
    assert resolve(_provider("chat", "openai_compatible", None)) == "responses"
    # Non-chat models have exactly one route each.
    assert resolve(_provider("embedding", "openai_compatible", None)) == "embeddings"
    assert resolve(_provider("reranker", "openai_compatible", None)) == "rerank"


class _StubSession:
    """A session whose provider lookup returns exactly one prepared row."""

    def __init__(self, provider):
        self._provider = provider

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def query(self, *_models):
        return self

    def filter(self, *_conditions):
        return self

    def first(self):
        return self._provider


def test_a_responses_model_reaches_the_gateway_instead_of_404(monkeypatch):
    """The whole route, not just the path helper.

    The 404 came from this route's own guard — it compares the path the client
    asked for against the one the gateway derived. A Responses-protocol model
    asks for ``/responses`` while the gateway derived ``chat/completions``, so
    the request never reached any upstream. On the desktop that surfaced as a
    silent failover onto whichever model does speak chat completions.
    """
    from types import SimpleNamespace

    captured: dict = {}

    class EventStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.completed"}\n\n'

    provider = SimpleNamespace(
        provider_id="provider-1",
        provider="openai_compatible",
        provider_type="chat",
        model_name="deepseekv4-flash-vision",
        base_url="http://192.0.2.10:1029/v1",
        api_key="real-cloud-key",
        is_active=True,
        extra_config={"api_protocol": "responses"},
    )

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=EventStream()
        )

    monkeypatch.setattr(service, "SessionLocal", lambda: _StubSession(provider))
    monkeypatch.setattr(service, "_model_provider_allowed", lambda *_a, **_k: True)
    # 出口脱敏集合与本用例无关；品牌分支在这里还会查凭据策略，测试环境没有。
    monkeypatch.setattr(service, "gateway_stream_secrets", lambda *_a, **_k: set())
    monkeypatch.setattr(
        routes, "_gateway_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    )

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_capability_user] = lambda: "user-1"
    with TestClient(app) as client:
        response = client.post(
            "/v1/desktop/capability/gateway/models/provider-1/responses",
            headers={"authorization": "Bearer desktop-capability-token"},
            json={"model": "whatever-the-client-said", "input": "hi"},
        )

    assert response.status_code == 200, "Responses 协议的模型不应再被网关判成 404"
    assert captured["url"] == "http://192.0.2.10:1029/v1/responses"


def test_a_chat_completions_model_still_rejects_a_responses_request(monkeypatch):
    """The guard itself stays: a client may not pick the upstream path."""
    from types import SimpleNamespace

    provider = SimpleNamespace(
        provider_id="provider-2",
        provider="zhipu",
        provider_type="chat",
        model_name="glm-5.3",
        base_url="http://192.0.2.11:1029/v1",
        api_key="k",
        is_active=True,
        extra_config={"api_protocol": "chat_completions"},
    )
    monkeypatch.setattr(service, "SessionLocal", lambda: _StubSession(provider))
    monkeypatch.setattr(service, "_model_provider_allowed", lambda *_a, **_k: True)

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes._require_capability_user] = lambda: "user-1"
    with TestClient(app) as client:
        response = client.post(
            "/v1/desktop/capability/gateway/models/provider-2/responses",
            headers={"authorization": "Bearer desktop-capability-token"},
            json={"input": "hi"},
        )

    assert response.status_code == 404
