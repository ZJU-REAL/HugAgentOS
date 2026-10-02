"""Thinking settings round trip through the administrator API."""

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api.deps import require_system_settings
from api.routes.v1.models import router
from core.db.engine import get_db


def test_model_settings_save_read_and_reject_invalid_default(db_session, monkeypatch):
    original = httpx.AsyncClient

    def reply(request):
        return httpx.Response(200, json={"id": "ok", "choices": []})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(reply), **kwargs),
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_system_settings] = lambda: None
    app.dependency_overrides[get_db] = lambda: db_session
    payload = {
        "display_name": "Vision",
        "provider_type": "chat",
        "provider": "openai_compatible",
        "base_url": "http://model.test/v1",
        "api_key": "secret",
        "model_name": "vision",
        "extra_config": {
            "context_length": 65536,
            "api_protocol": "responses",
            "supports_reasoning_effort": True,
            "reasoning_effort_levels": [
                {"key": "high", "value": "high"},
                {"key": "max", "value": 100},
            ],
            "default_reasoning_effort": "high",
        },
    }
    with TestClient(app) as client:
        created = client.post("/v1/models/providers", json=payload)
        assert created.status_code == 200, created.text
        rows = client.get("/v1/models/providers").json()["data"]
        assert rows[0]["extra_config"] == payload["extra_config"]
        assert rows[0]["api_key"] != "secret"
        provider_id = rows[0]["provider_id"]
        invalid = {**payload["extra_config"], "default_reasoning_effort": "medium"}
        response = client.put(f"/v1/models/providers/{provider_id}", json={"extra_config": invalid})
        assert response.status_code == 400
        assert (
            client.get("/v1/models/providers").json()["data"][0]["extra_config"][
                "default_reasoning_effort"
            ]
            == "high"
        )
