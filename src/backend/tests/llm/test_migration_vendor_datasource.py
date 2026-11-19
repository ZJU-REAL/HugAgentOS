"""Migration seams outside the main factory: LiteLLM."""
import sys
from types import SimpleNamespace

import pytest
from agentscope.message import UserMsg
from agentscope.model import OpenAIChatModel
from openai.types.chat import ChatCompletionChunk

from core.models.providers.vendor_models import LiteLLMChatModel


@pytest.mark.asyncio
async def test_litellm_stream_uses_native_parser(monkeypatch):
    calls = []
    closed = []

    async def completion(**kwargs):
        calls.append(kwargs)

        async def stream():
            for delta, finish in [({"role": "assistant", "content": "vendor-ok"}, None), ({}, "stop")]:
                yield ChatCompletionChunk.model_validate({
                    "id": "litellm-stream", "object": "chat.completion.chunk",
                    "created": 1, "model": "vendor-probe",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                })
        class VendorStream:
            def __aiter__(self):
                return stream()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                closed.append(True)

        return VendorStream()

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=completion))
    model = LiteLLMChatModel(
        litellm_model="bedrock/vendor-probe", litellm_kwargs={}, model="vendor-probe",
        parameters=OpenAIChatModel.Parameters(), stream=True, timeout=5, context_size=32768,
    )
    response = await model._call_api("vendor-probe", [UserMsg(name="user", content="hello")])
    chunks = [chunk async for chunk in response]
    assert calls[0]["stream"] is True
    assert calls[0]["model"] == "bedrock/vendor-probe"
    assert any("vendor-ok" in str(chunk.content) for chunk in chunks)
    assert chunks[-1].finished_reason.value == "completed"
    assert closed == [True]
