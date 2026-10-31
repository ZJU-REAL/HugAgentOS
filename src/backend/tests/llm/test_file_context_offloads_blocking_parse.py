"""Attachment injection must not block the event loop.

Regression pin for the "streaming stopped working" incident: attaching an 8MB PDF
froze the entire backend for 79 seconds because the attachment hook parsed it
inline. Document attachments are now injected as file_id handles without any
I/O (``read_artifact`` parses on demand in a worker thread); images still need a
download per file, so that work must stay off the loop. These tests pin the
*behaviour* (the loop keeps running) rather than the call syntax.
"""

import asyncio
import time
import types

import pytest

from core.llm.context import attachment_hook as mw
from tests.orchestration.hook_support import inject_attachments

# How long the fake "parse" blocks its thread. Long enough that a blocked loop is
# unambiguous, short enough to keep the suite fast.
BLOCKING_SECONDS = 0.5
# The heartbeat ticks every 10ms, so an unblocked loop gets ~50 ticks. Assert on a
# fraction of that to stay clear of scheduler jitter on a loaded CI box.
MIN_EXPECTED_TICKS = 10


def _make_agent(uploaded_files=None):
    """Minimal agent stand-in: the middleware only touches ``agent.state``."""
    state = types.SimpleNamespace(
        uploaded_files=list(uploaded_files or []),
        historical_files=[],
        user_id="user_test",
        context=[],
    )
    return types.SimpleNamespace(state=state)


async def _count_heartbeats_during(coro) -> int:
    """Run ``coro`` while ticking a 10ms heartbeat; return the tick count.

    A tick can only happen when the event loop is free to schedule it, so the count
    is a direct measure of whether ``coro`` monopolised the loop.
    """
    ticks = 0
    stop = False

    async def heartbeat():
        nonlocal ticks
        while not stop:
            await asyncio.sleep(0.01)
            ticks += 1

    hb = asyncio.create_task(heartbeat())
    try:
        await coro
    finally:
        stop = True
        hb.cancel()
        try:
            await hb
        except asyncio.CancelledError:
            pass
    return ticks


@pytest.mark.asyncio
async def test_native_image_injection_does_not_block_the_event_loop(monkeypatch):
    """Image download + base64 encode also runs off the loop.

    Same defect class: on S3/OSS deployments each image is a network round-trip.
    """
    monkeypatch.setattr(mw, "supports_native_images", lambda st: True)

    def fake_inject_native_images(image_files, user_id):
        time.sleep(BLOCKING_SECONDS)
        return [{"type": "text", "text": "images-injected"}]

    monkeypatch.setattr(
        mw,
        "native_image_content",
        fake_inject_native_images,
    )
    monkeypatch.setattr(mw.file_context, "build_file_context", lambda files: "")

    agent = _make_agent([{"file_id": "ua_img", "name": "a.png", "mime_type": "image/png"}])
    ticks = await _count_heartbeats_during(inject_attachments(agent))

    assert any("images-injected" in str(getattr(m, "content", "")) for m in agent.state.context)
    assert (
        ticks >= MIN_EXPECTED_TICKS
    ), f"event loop stalled during image injection: only {ticks} heartbeat(s)"
