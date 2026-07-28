"""Tests for the dedicated OpenAI/Codex provider preset."""

from core.llm.chat_models import make_chat_model
from core.llm.providers.registry import get_spec, to_frontend_schema


def _make_model(provider: str, reasoning_effort: str | None):
    return make_chat_model(
        model="test-model",
        temperature=0.0,
        max_tokens=32,
        timeout=10,
        base_url="http://model.test/api/v1",
        api_key="test-key",
        provider=provider,
        reasoning_effort=reasoning_effort,
        stream=True,
        context_size=4096,
    )


def test_openai_provider_is_exposed_to_dynamic_forms():
    spec = get_spec("openai")

    assert spec.label == "OpenAI / Codex"
    assert spec.reasoning_effort_top_level is True
    assert any(row["id"] == "openai" for row in to_frontend_schema())


def test_openai_provider_sends_top_level_reasoning_effort():
    model = _make_model("openai", "high")

    assert model._extra_body["reasoning_effort"] == "high"
    assert model._extra_body["chat_template_kwargs"] == {
        "thinking": True,
        "reasoning_effort": "high",
    }


def test_generic_compatible_provider_keeps_existing_reasoning_transport():
    model = _make_model("openai_compatible", "high")

    assert "reasoning_effort" not in model._extra_body
    assert model._extra_body["chat_template_kwargs"] == {
        "thinking": True,
        "reasoning_effort": "high",
    }
