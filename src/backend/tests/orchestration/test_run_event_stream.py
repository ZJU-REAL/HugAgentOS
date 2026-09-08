"""Both run-event-log backends must behave identically.

The follower resumes by cursor and parks for the next event, so any
disagreement between the backends shows up as a chat that renders nothing
until it finishes — the failure mode that motivated this seam.
"""

from __future__ import annotations

import asyncio
import threading
import time

import fakeredis.aioredis
import pytest

from orchestration.run_event_stream import (
    START,
    LocalRunEventStream,
    RedisRunEventStream,
)


@pytest.fixture(params=["local", "redis"])
def stream(request, monkeypatch):
    if request.param == "local":
        return LocalRunEventStream()
    client = fakeredis.aioredis.FakeRedis(decode_responses=True, protocol=2)
    monkeypatch.setattr("orchestration.run_event_stream.get_redis", lambda **_kwargs: client)
    return RedisRunEventStream()


async def _seed(stream, run_id, count):
    for offset in range(1, count + 1):
        await stream.append(run_id, {"type": "content", "_offset": offset})
    return await stream.read(run_id)


@pytest.mark.asyncio
async def test_read_replays_in_order_from_the_start(stream):
    entries = await _seed(stream, "run", 3)
    assert [event["_offset"] for _cursor, event in entries] == [1, 2, 3]


@pytest.mark.asyncio
async def test_read_after_a_cursor_excludes_what_was_seen(stream):
    """This is resume: a reconnecting follower must not replay what it already got."""
    entries = await _seed(stream, "run", 3)
    after_first = await stream.read("run", after=entries[0][0])
    assert [event["_offset"] for _cursor, event in after_first] == [2, 3]
    assert await stream.read("run", after=entries[-1][0]) == []


@pytest.mark.asyncio
async def test_read_honours_a_limit(stream):
    await _seed(stream, "run", 5)
    assert len(await stream.read("run", after=START, limit=2)) == 2


@pytest.mark.asyncio
async def test_wait_returns_immediately_when_events_are_already_there(stream):
    entries = await _seed(stream, "run", 2)
    started = time.monotonic()
    batch = await stream.wait("run", after=entries[0][0], limit=10, timeout_ms=3000)
    assert [event["_offset"] for _cursor, event in batch] == [2]
    assert time.monotonic() - started < 1.0


@pytest.mark.asyncio
async def test_wait_wakes_on_a_later_append(stream):
    entries = await _seed(stream, "run", 1)

    async def writer():
        await asyncio.sleep(0.2)
        await stream.append("run", {"type": "content", "_offset": 2})

    task = asyncio.create_task(writer())
    started = time.monotonic()
    batch = await stream.wait("run", after=entries[-1][0], limit=10, timeout_ms=3000)
    waited = time.monotonic() - started
    await task

    assert [event["_offset"] for _cursor, event in batch] == [2]
    assert waited < 1.0, f"should wake on the append, waited {waited:.2f}s"


@pytest.mark.asyncio
async def test_wait_gives_up_quietly_on_a_silent_run(stream):
    entries = await _seed(stream, "run", 1)
    started = time.monotonic()
    assert await stream.wait("run", after=entries[-1][0], limit=10, timeout_ms=300) == []
    assert time.monotonic() - started >= 0.25


@pytest.mark.asyncio
async def test_last_write_ms_tracks_the_newest_event(stream):
    """The stale-run reaper reads this to decide whether a run is still moving."""
    assert await stream.last_write_ms("quiet") is None
    before = int(time.time() * 1000)
    await _seed(stream, "run", 1)
    written = await stream.last_write_ms("run")
    assert written is not None and written >= before - 1000


@pytest.mark.asyncio
async def test_clear_drops_a_crashed_runs_projection(stream):
    await _seed(stream, "run", 2)
    await stream.clear("run")
    assert await stream.read("run") == []
    assert await stream.last_write_ms("run") is None


@pytest.mark.asyncio
async def test_expire_eventually_removes_the_log(stream):
    await _seed(stream, "run", 1)
    await stream.expire("run", 1)
    assert await stream.read("run") != []
    await asyncio.sleep(1.1)
    assert await stream.read("run") == []


@pytest.mark.asyncio
async def test_cursors_stay_ordered_across_many_appends(stream):
    """Same-millisecond appends must still resume in order, not collapse."""
    entries = await _seed(stream, "run", 50)
    cursors = [cursor for cursor, _event in entries]
    assert len(set(cursors)) == 50
    for earlier, later in zip(cursors, cursors[1:]):
        assert await stream.read("run", after=earlier, limit=1) != []
        assert earlier != later


@pytest.mark.asyncio
async def test_local_wait_wakes_from_another_event_loop():
    """Sub-agents run on their own loop, so the writer often is not the reader's."""
    stream = LocalRunEventStream()
    entries = await _seed(stream, "run", 1)

    def writer():
        async def run():
            await asyncio.sleep(0.2)
            await stream.append("run", {"type": "content", "_offset": 2})

        asyncio.run(run())

    threading.Thread(target=writer, daemon=True).start()
    started = time.monotonic()
    batch = await stream.wait("run", after=entries[-1][0], limit=10, timeout_ms=3000)
    waited = time.monotonic() - started

    assert [event["_offset"] for _cursor, event in batch] == [2]
    assert waited < 1.0, f"cross-loop wake took {waited:.2f}s"
