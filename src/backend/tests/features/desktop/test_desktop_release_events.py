"""Release notifications through the subscription interface and public HTTP stream."""
import asyncio
import json
import pytest

from features.desktop.bridge.release_events import ReleaseEvents


@pytest.mark.asyncio
async def test_subscribers_get_initial_and_changed_revision_without_duplicate_events(tmp_path):
    releases = ReleaseEvents(tmp_path, interval=0.01)
    async with releases.subscribe() as first, releases.subscribe() as second:
        initial = await asyncio.wait_for(first.get(), 1)
        assert await asyncio.wait_for(second.get(), 1) == initial
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(first.get(), 0.04)
        (tmp_path / "release-index.json").write_text(json.dumps({"latest": {}}))
        changed = await asyncio.wait_for(first.get(), 1)
        assert changed != initial
        assert await asyncio.wait_for(second.get(), 1) == changed
        # The authoritative index suppresses legacy mirror-only changes.
        (tmp_path / "latest.json").write_text("{}")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(first.get(), 0.04)
    async with releases.subscribe() as reconnected:
        assert await asyncio.wait_for(reconnected.get(), 1) == changed


@pytest.mark.asyncio
async def test_slow_subscriber_gets_latest_revision_without_queue_growth(tmp_path):
    releases = ReleaseEvents(tmp_path, interval=0.01)
    async with releases.subscribe() as fast, releases.subscribe() as slow:
        await asyncio.wait_for(fast.get(), 1)
        for value in ("one", "two", "three"):
            pending = tmp_path / "release-index.pending"
            pending.write_text(value)
            pending.replace(tmp_path / "release-index.json")
            latest = await asyncio.wait_for(fast.get(), 1)
        assert slow.qsize() == 1
        assert slow.get_nowait() == latest


@pytest.mark.asyncio
async def test_public_http_stream_sends_release_changes_without_reconnecting(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from features.desktop.api.desktop import router

    app = FastAPI()
    app.include_router(router)
    app.state.desktop_release_events = ReleaseEvents(tmp_path, interval=0.01)
    monkeypatch.setenv("DESKTOP_RELEASE_DIR", str(tmp_path))
    sent = asyncio.Queue()
    disconnect = asyncio.Event()
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    task = asyncio.create_task(app({
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "method": "GET", "scheme": "http", "path": "/v1/desktop/events",
        "raw_path": b"/v1/desktop/events", "query_string": b"",
        "root_path": "", "headers": [], "server": ("test", 80),
        "client": ("127.0.0.1", 32100), "http_version": "1.1",
    }, receive, sent.put))
    try:
        start = await asyncio.wait_for(sent.get(), 1)
        assert start["status"] == 200
        headers = dict(start["headers"])
        assert headers[b"content-type"].startswith(b"text/event-stream")
        assert headers[b"x-accel-buffering"] == b"no"
        first = await asyncio.wait_for(sent.get(), 1)
        assert b'event: release' in first["body"]
        assert b'unpublished' in first["body"]
        (tmp_path / "release-index.json").write_text('{"latest":{}}')
        changed = await asyncio.wait_for(sent.get(), 1)
        assert changed["body"] != first["body"]
        assert b'event: release' in changed["body"]
        assert not task.done(), "the same stream remains open after publication"
    finally:
        disconnect.set()
        await asyncio.wait_for(task, 1)
