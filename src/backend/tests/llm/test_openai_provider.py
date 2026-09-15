"""Tests for OpenAI-compatible model providers."""

from core.llm.chat_models import make_chat_model
from core.llm.providers.registry import get_spec, to_frontend_schema


def _make_model(
    provider: str, reasoning_effort: str | None, api_protocol: str = "chat_completions"
):
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
        api_protocol=api_protocol,
    )


def test_openai_provider_is_exposed_to_dynamic_forms():
    spec = get_spec("openai")

    assert spec.label == "OpenAI / Codex"
    assert spec.reasoning_effort_top_level is True
    assert spec.structured_reasoning is True
    assert any(row["id"] == "openai" for row in to_frontend_schema())


def test_openai_provider_sends_top_level_reasoning_effort():
    model = _make_model("openai", "high")

    assert model._extra_body["reasoning_effort"] == "high"
    assert model._extra_body["chat_template_kwargs"] == {
        "thinking": True,
        "reasoning_effort": "high",
    }
    assert model.structured_reasoning is True


def test_generic_compatible_provider_keeps_existing_reasoning_transport():
    model = _make_model("openai_compatible", "high")

    assert "reasoning_effort" not in model._extra_body
    assert model._extra_body["chat_template_kwargs"] == {
        "thinking": True,
        "reasoning_effort": "high",
    }
    assert model.structured_reasoning is False


def test_unrecorded_protocol_defaults_to_responses():
    """Responses is the default; chat completions is only for endpoints found to lack it."""
    from core.llm.responses_models import OpenAICompatResponsesModel

    model = _make_model("openai_compatible", None, api_protocol=None)

    assert isinstance(model, OpenAICompatResponsesModel)
    assert model.wire_protocol == "openai_responses"
    # Reasoning arrives as its own item on this wire, never inline in the body.
    assert model.structured_reasoning is True


def test_probed_chat_only_endpoint_stays_on_chat_completions():
    from core.llm.chat_models import OpenAICompatChatModel

    model = _make_model("openai_compatible", None, api_protocol="chat_completions")

    assert isinstance(model, OpenAICompatChatModel)
    assert model.wire_protocol == "openai_chat"


def test_azure_never_uses_responses():
    """Azure addresses Responses per deployment, which the shared client cannot build."""
    from core.llm.chat_models import OpenAICompatChatModel

    model = make_chat_model(
        model="test-model",
        temperature=0.0,
        max_tokens=32,
        timeout=10,
        base_url="https://res.openai.azure.com",
        api_key="test-key",
        provider="azure_openai",
        provider_extra={"api_version": "2024-06-01", "deployment": "gpt-4o"},
        stream=True,
        context_size=4096,
        api_protocol="responses",
    )

    assert isinstance(model, OpenAICompatChatModel)
