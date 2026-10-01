"""Per-model effort settings at configuration and model-request boundaries."""

import json
import httpx
import pytest
from agentscope.message import Msg, TextBlock
from core.db.models import ModelProvider
from core.llm.chat_models import build_model_for_mode
from core.llm.providers.registry import validate_payload
from core.services.model_config import ResolvedModelConfig
from core.services.user_model_selection import list_user_selectable_models

CONFIG = {
    "supports_reasoning_effort": True,
    "reasoning_effort_levels": [
        {"key": "low", "value": "low"},
        {"key": "high", "value": "high"},
        {"key": "xhigh", "value": "xhigh"},
        {"key": "max", "value": 100},
    ],
    "default_reasoning_effort": "high",
}


def test_config_rejects_a_default_not_in_enabled_levels():
    invalid = {**CONFIG, "default_reasoning_effort": "medium"}
    assert validate_payload("openai_compatible", "chat", invalid) is not None


def test_selectable_model_exposes_configured_levels_without_credentials(db_session):
    db_session.add(
        ModelProvider(
            provider_id="effort-model",
            display_name="Effort",
            provider_type="chat",
            provider="openai_compatible",
            base_url="http://model.test/v1",
            api_key="secret",
            model_name="effort",
            extra_config=CONFIG,
            is_active=True,
        )
    )
    db_session.commit()
    row = list_user_selectable_models(db_session)[0]
    assert row["reasoning_effort_levels"] == CONFIG["reasoning_effort_levels"]
    assert row["default_reasoning_effort"] == "high"
    assert "api_key" not in row and "base_url" not in row


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,wire", [("medium", "high"), ("max", 100), ("xhigh", "xhigh")])
async def test_responses_sends_configured_mapping_and_valid_default(monkeypatch, mode, wire):
    monkeypatch.setattr("core.llm.context_manager.resolve_model_context_window", lambda name: 65536)
    cfg = ResolvedModelConfig(
        base_url="http://model.test/v1",
        api_key="test-key",
        model_name="effort",
        extra={**CONFIG, "api_protocol": "responses"},
    )
    captured = []

    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "effort",
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1},
            },
        )

    model = build_model_for_mode(cfg, mode=mode, stream=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    model._http_client = client
    try:
        await model._call_api(
            "effort", [Msg(name="user", role="user", content=[TextBlock(text="hello")])]
        )
    finally:
        await client.aclose()
    assert captured[0]["reasoning"]["effort"] == wire
