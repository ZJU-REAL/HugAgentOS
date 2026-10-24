"""Closing the public stream synchronously unwinds its inner generator."""
from contextvars import ContextVar
from types import SimpleNamespace
import pytest
from core.llm.runtime import stream_loop
from core.llm.runtime.streaming import StreamingAgent


@pytest.mark.asyncio
async def test_close_after_first_event_finishes_cleanup_in_same_context(monkeypatch):
    active = ContextVar("active_stream", default=False)
    cleaned = []

    async def inner(*args, **kwargs):
        token = active.set(True)
        try:
            yield ("text_delta", "first")
            pytest.fail("Closing must not request another event")
        finally:
            active.reset(token)
            cleaned.append(True)

    monkeypatch.setattr(stream_loop, "stream", inner)
    wrapper = StreamingAgent(SimpleNamespace(), [])
    stream = wrapper.stream([], {})
    assert await anext(stream) == ("text_delta", "first")
    assert active.get() is True
    await stream.aclose()
    assert cleaned == [True]
    assert active.get() is False
