"""Edition-neutral bounded blocking-lane contract."""

import asyncio
import time

import pytest


@pytest.mark.asyncio
async def test_blocking_lane_keeps_slot_until_timed_out_thread_finishes():
    from mcp_servers.retrieve_dataset_content_mcp.server import _BlockingLane

    lane = _BlockingLane(name="test", max_workers=1)

    with pytest.raises(TimeoutError):
        await lane.run(lambda: time.sleep(0.08), timeout=0.02)

    with pytest.raises(TimeoutError):
        await lane.run(lambda: "too-early", timeout=0.02)

    await asyncio.sleep(0.07)
    assert await lane.run(lambda: "done", timeout=0.05) == "done"
