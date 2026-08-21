"""FileContextMiddleware must not parse attachments on the event loop.

Regression pin for the "streaming stopped working" incident: attaching an 8MB PDF
froze the entire backend for 79 seconds. ``FileContextMiddleware._inject`` is
``async``, but it called ``_build_file_context`` inline — and on a parse-cache miss
that helper POSTs the document to the external file-parser service with blocking
``requests``. The single event loop that also drives every open SSE stream was
pinned for the whole parse: no chat produced a token, queued requests all landed in
the same millisecond once it returned, and even ``/health`` stalled.

The fix hands that work to a worker thread. These tests pin the *behaviour* (the
loop keeps running) rather than the call syntax, so they stay meaningful if the
offload is reimplemented some other way.
"""

import asyncio
import time
import types

import pytest

from core.llm import middlewares as mw

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
async def test_text_attachment_parse_does_not_block_the_event_loop(monkeypatch):
    """The blocking parse of a text/PDF attachment runs off the loop."""
    called = {}

    def fake_build_file_context(uploaded_files, user_id=None):
        # Stands in for the blocking requests.post to the file-parser service.
        called["user_id"] = user_id
        called["n"] = len(uploaded_files)
        time.sleep(BLOCKING_SECONDS)
        return "[file content begin]parsed[file content end]"

    monkeypatch.setattr(mw, "_build_file_context", fake_build_file_context)

    agent = _make_agent([{"file_id": "ua_x", "name": "a.pdf", "mime_type": "application/pdf"}])
    ticks = await _count_heartbeats_during(mw.FileContextMiddleware()._inject(agent))

    # The parse actually ran, with ownership still propagated.
    assert called["n"] == 1
    assert called["user_id"] == "user_test"
    # ...and its result reached the model context.
    assert any("parsed" in str(getattr(m, "content", "")) for m in agent.state.context)
    # ...without the loop going dark. Inline, ticks would be 0.
    assert ticks >= MIN_EXPECTED_TICKS, (
        f"event loop stalled during attachment parse: only {ticks} heartbeat(s) in "
        f"{BLOCKING_SECONDS}s — the blocking parse is back on the loop"
    )


@pytest.mark.asyncio
async def test_native_image_injection_does_not_block_the_event_loop(monkeypatch):
    """Image download + base64 encode also runs off the loop.

    Same defect class: on S3/OSS deployments each image is a network round-trip.
    """
    monkeypatch.setattr(mw, "_effective_model_supports_vision", lambda st: True)

    def fake_inject_native_images(st, image_files, user_id):
        time.sleep(BLOCKING_SECONDS)
        st.context.append("images-injected")

    monkeypatch.setattr(
        mw.FileContextMiddleware,
        "_inject_native_images",
        staticmethod(fake_inject_native_images),
    )
    monkeypatch.setattr(mw, "_build_file_context", lambda files, user_id=None: "")

    agent = _make_agent([{"file_id": "ua_img", "name": "a.png", "mime_type": "image/png"}])
    ticks = await _count_heartbeats_during(mw.FileContextMiddleware()._inject(agent))

    assert "images-injected" in agent.state.context
    assert (
        ticks >= MIN_EXPECTED_TICKS
    ), f"event loop stalled during image injection: only {ticks} heartbeat(s)"
