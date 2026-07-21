from __future__ import annotations

import asyncio
import time

import pytest
from orchestration.chat_run_executor import _aiter_with_inactivity_timeout


def _is_meaningful(chunk: dict) -> bool:
    return chunk.get("type") != "heartbeat"


@pytest.mark.asyncio
async def test_heartbeat_does_not_mask_a_hung_workflow():
    closed = False

    async def _heartbeats():
        nonlocal closed
        try:
            while True:
                await asyncio.sleep(0.01)
                yield {"type": "heartbeat"}
        finally:
            closed = True

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="无有效输出"):
        async for _ in _aiter_with_inactivity_timeout(
            _heartbeats(),
            0.05,
            is_activity=_is_meaningful,
        ):
            pass

    assert time.monotonic() - started < 0.2
    assert closed is True


@pytest.mark.asyncio
async def test_meaningful_output_resets_the_deadline():
    async def _chunks():
        await asyncio.sleep(0.01)
        yield {"type": "heartbeat"}
        await asyncio.sleep(0.01)
        yield {"type": "tool_call"}
        await asyncio.sleep(0.03)
        yield {"type": "tool_result"}

    chunks = [
        chunk
        async for chunk in _aiter_with_inactivity_timeout(
            _chunks(),
            0.04,
            is_activity=_is_meaningful,
        )
    ]
    assert [chunk["type"] for chunk in chunks] == ["heartbeat", "tool_call", "tool_result"]
