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
