"""Tests for OpenAI-compatible model providers."""

from types import SimpleNamespace

import pytest
from agentscope.message import (
    Base64Source,
    DataBlock,
    Msg,
    TextBlock,
    ToolResultBlock,
    ToolResultState,
)
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


@pytest.mark.asyncio
async def test_text_only_model_retries_tool_image_result_without_multimodal_blocks(monkeypatch):
    """A figure-returning MCP must not end the ReAct loop on a text-only model."""
    model = make_chat_model(
        model="deepseekv4-flash",
        temperature=0.0,
        max_tokens=32,
        timeout=10,
        base_url="http://model.test/api/v1",
        api_key="test-key",
        provider="openai_compatible",
        stream=False,
        context_size=4096,
    )
    tool_result = ToolResultBlock(
        id="call-figures",
        name="get_paper_figures",
        output=[
            TextBlock(type="text", text='{"title":"paper","figures":[{"caption":"architecture"}]}'),
            DataBlock(
                type="data",
                source=Base64Source(
                    type="base64",
                    media_type="image/png",
                    data="QUJD",
                ),
            ),
        ],
        state=ToolResultState.SUCCESS,
    )
    messages = [Msg(name="assistant", role="assistant", content=[tool_result])]

    calls: list[dict] = []
    expected_response = object()

    async def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("deepseekv4-flash is not a multimodal model")
        return expected_response

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    monkeypatch.setattr(model, "_build_client", lambda: fake_client)
    monkeypatch.setattr(
        model,
        "_parse_completion_response",
        lambda _started_at, response, _audio_format: response,
    )

    response = await model._call_api("deepseekv4-flash", messages)

    assert response is expected_response
    assert len(calls) == 2
    assert "image_url" in str(calls[0]["messages"])
    assert "image_url" not in str(calls[1]["messages"])
    assert "architecture" in str(calls[1]["messages"])
