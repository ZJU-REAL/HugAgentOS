"""Vendor request and stream-read timeout contracts without paid API calls."""

import asyncio
import pytest
from core.llm.providers.registry import get_spec
from core.llm.providers.vendor_models import build_native_model, build_litellm_model
from agentscope.model import (
    AnthropicChatModel,
    GeminiChatModel,
    DashScopeChatModel,
    OllamaChatModel,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "vendor,base",
    [
        ("anthropic", AnthropicChatModel),
        ("gemini", GeminiChatModel),
        ("dashscope", DashScopeChatModel),
        ("ollama", OllamaChatModel),
    ],
)
@pytest.mark.parametrize("stage", ["headers", "stream"])
async def test_native_vendor_honors_configured_timeout(monkeypatch, vendor, base, stage):
    closed = []

    async def call(self, *args, **kwargs):
        if stage == "headers":
            await asyncio.Event().wait()

        async def stream():
            try:
                yield "first"
                await asyncio.Event().wait()
            finally:
                closed.append(True)

        return stream()

    monkeypatch.setattr(base, "_call_api", call)
    model = build_native_model(
        get_spec(vendor),
        model="test",
        temperature=0.2,
        max_tokens=30,
        base_url="http://example.invalid",
        api_key="test",
        context_size=8192,
        stream=True,
        timeout=0.02,
    )
    with pytest.raises(TimeoutError):
        response = await model._call_api("test", [])
        assert await anext(response) == "first"
        await anext(response)
    if stage == "stream":
        assert closed == [True]


def test_litellm_timeout_is_not_raised_to_600_seconds():
    model = build_litellm_model(
        get_spec("bedrock"),
        model="test",
        temperature=0.2,
        max_tokens=30,
        timeout=7,
        provider_extra={},
        context_size=8192,
        stream=True,
    )
    assert model._timeout == 7
