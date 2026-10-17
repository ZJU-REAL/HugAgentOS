"""Scheduling must isolate slow approvals without replaying overlapping changes."""
import asyncio
import time

import pytest

from core.space_sync.events import Change
from core.space_sync.personal import MySpaceRegistry
from core.space_sync.personal_dispatch import MAX_ACTIVE_TASKS


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def queued(registry, path, change=None):
    key = ("alice", path)
    registry._mark(key)
    if change:
        registry._events[key] = change
    return key


@pytest.mark.asyncio
async def test_unrelated_path_progresses_while_approval_waits(monkeypatch):
    registry = MySpaceRegistry()
    waiting = asyncio.Event()
    started = []

    async def process(user, rels, **kwargs):
        started.extend(rels)
        if rels == ["private/a"]:
            await waiting.wait()

    monkeypatch.setattr(registry, "_process", process)
    first = queued(registry, "private/a")
    independent = queued(registry, "public/b")
    child = queued(registry, "private/a/child")
    registry._dispatch([first, independent, child], time.monotonic(), force=True)
    await settle()
    assert "public/b" in started
    assert child in registry._pending
    assert "private/a/child" not in started
    waiting.set()
    await asyncio.gather(*registry._tasks)
    await settle()
    registry._dispatch([child], time.monotonic(), force=True)
    await asyncio.gather(*registry._tasks)
    assert "private/a/child" in started
    assert not registry._active_paths


@pytest.mark.asyncio
async def test_move_reserves_both_subtrees_and_parent_runs_first(monkeypatch):
    registry = MySpaceRegistry()
    release = asyncio.Event()
    started = []

    async def process(user, rels, **kwargs):
        started.extend(rels)
        await release.wait()

    monkeypatch.setattr(registry, "_process", process)
    parent = queued(registry, "New", Change("moved", "Old", "New", True))
    old_child = queued(registry, "Old/a")
    new_child = queued(registry, "New/a")
    nested_move = queued(registry, "Other", Change("moved", "Old/sub", "Other", True))
    independent = queued(registry, "Elsewhere/a")
    registry._dispatch([old_child, new_child, nested_move, independent, parent], time.monotonic())
    await settle()
    assert set(started) == {"New", "Elsewhere/a"}
    assert all(key in registry._pending for key in (old_child, new_child, nested_move))
    release.set()
    await asyncio.gather(*registry._tasks)
    assert not registry._active_paths


@pytest.mark.asyncio
async def test_large_queue_creates_only_bounded_tasks_and_keeps_journal(monkeypatch, tmp_path):
    from core.space_sync.journal import Journal
    registry = MySpaceRegistry()
    registry._journal = Journal(tmp_path / "events.sqlite")
    release = asyncio.Event()

    async def process(*args, **kwargs):
        await release.wait()

    monkeypatch.setattr(registry, "_process", process)
    for i in range(1000):
        key = queued(registry, f"file{i}")
        change = Change("modified", key[1])
        registry._versions[key] = registry._journal.put("alice:" + key[1], change)
        registry._events[key] = change
    tasks = registry._dispatch(list(registry._pending), time.monotonic())
    assert len(tasks) == MAX_ACTIVE_TASKS
    assert len(registry._pending) == 1000 - MAX_ACTIVE_TASKS
    assert len(registry._journal.pending()) == 1000
    assert registry._dispatch(list(registry._pending), time.monotonic()) == []
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    # Cancellation before coroutine entry cannot run its finally; completion
    # callbacks still release reservations and the journal retains all events.
    await settle()
    assert len(registry._journal.pending()) == 1000
    assert not registry._active_paths
    assert not registry._inflight
    assert len(registry._pending) == 1000
    registry._journal.close()


@pytest.mark.asyncio
async def test_new_generation_waits_for_same_path_and_is_not_acknowledged(monkeypatch):
    registry = MySpaceRegistry()
    release = asyncio.Event()
    seen = []

    async def process(user, rels, *, events, **kwargs):
        seen.append(events[(user, rels[0])].kind)
        await release.wait()

    monkeypatch.setattr(registry, "_process", process)
    key = queued(registry, "a", Change("modified", "a"))
    registry._versions[key] = 1
    tasks = registry._dispatch([key], time.monotonic())
    # Replace the queued event before the first task even enters its coroutine.
    queued(registry, "a", Change("deleted", "a"))
    registry._versions[key] = 2
    assert registry._dispatch([key], time.monotonic()) == []
    await settle()
    assert seen == ["modified"]
    release.set()
    await asyncio.gather(*tasks)
    await settle()
    assert registry._events[key].kind == "deleted"
    tasks = registry._dispatch([key], time.monotonic())
    await asyncio.gather(*tasks)
    assert seen == ["modified", "deleted"]


@pytest.mark.asyncio
async def test_failure_releases_reservation_for_independent_success(monkeypatch):
    registry = MySpaceRegistry()
    successful = []

    async def process(user, rels, **kwargs):
        if rels == ["bad/a"]:
            raise RuntimeError("offline")
        successful.extend(rels)

    monkeypatch.setattr(registry, "_process", process)
    bad = queued(registry, "bad/a")
    good = queued(registry, "good/a")
    tasks = registry._dispatch([bad, good], time.monotonic())
    await asyncio.gather(*tasks)
    await settle()
    assert successful == ["good/a"]
    assert bad in registry._failures and bad in registry._pending
    assert good not in registry._pending
    assert not registry._active_paths


@pytest.mark.asyncio
async def test_root_projection_does_not_reserve_unrelated_paths(monkeypatch):
    registry = MySpaceRegistry()
    release = asyncio.Event()
    started = []

    async def process(user, rels, **kwargs):
        started.extend(rels)
        if rels == [""]:
            await release.wait()

    monkeypatch.setattr(registry, "_process", process)
    root = queued(registry, "", Change("refresh", ""))
    root_tasks = registry._dispatch([root], time.monotonic())
    await settle()
    other = queued(registry, "Independent/file")
    tasks = registry._dispatch([other], time.monotonic())
    await asyncio.gather(*tasks)
    assert "Independent/file" in started
    assert not root_tasks[0].done()
    release.set()
    await asyncio.gather(*root_tasks)


@pytest.mark.asyncio
async def test_parent_move_precedes_child_moved_to_shallow_destination(monkeypatch):
    registry = MySpaceRegistry()
    release = asyncio.Event()
    started = []

    async def process(user, rels, **kwargs):
        started.extend(rels)
        await release.wait()

    monkeypatch.setattr(registry, "_process", process)
    parent = queued(registry, "C/D", Change("moved", "A/B", "C/D", True))
    child = queued(registry, "X", Change("moved", "A/B/sub", "X", True))
    tasks = registry._dispatch([child, parent], time.monotonic())
    await settle()
    assert started == ["C/D"]
    assert child in registry._pending
    release.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_saturated_drain_waits_for_worker_wakeup(monkeypatch):
    registry = MySpaceRegistry()
    key = queued(registry, "a")
    calls = 0

    def dispatch(*args, **kwargs):
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(registry, "_dispatch", dispatch)
    registry._pending[key] = time.monotonic() - 1
    drain = asyncio.create_task(registry._drain_loop())
    await asyncio.sleep(0.04)
    assert calls == 1
    registry._wake.set()
    await settle()
    assert calls == 2
    registry._closed = True
    registry._wake.set()
    await drain


@pytest.mark.asyncio
async def test_projection_queue_cannot_fill_every_worker_slot(monkeypatch):
    from core.space_sync.personal_dispatch import MAX_ACTIVE_PROJECTIONS
    registry = MySpaceRegistry()
    release = asyncio.Event()

    async def process(user, rels, **kwargs):
        await release.wait()

    monkeypatch.setattr(registry, "_process", process)
    roots = [(f"user{i}", "") for i in range(100)]
    for key in roots:
        registry._mark(key)
        registry._events[key] = Change("refresh", "")
    good = queued(registry, "independent")
    tasks = registry._dispatch(roots + [good], time.monotonic())
    assert len(tasks) == MAX_ACTIVE_PROJECTIONS + 1
    assert good in registry._inflight
    assert len(registry._pending) == 100 - MAX_ACTIVE_PROJECTIONS
    release.set()
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_file_move_precedes_stale_source_event_that_looks_deleted(monkeypatch):
    registry = MySpaceRegistry()
    release = asyncio.Event()
    started = []

    async def process(user, rels, **kwargs):
        started.extend(rels)
        await release.wait()

    monkeypatch.setattr(registry, "_process", process)
    source = queued(registry, "Moved/a.txt", Change("modified", "Moved/a.txt"))
    destination = queued(registry, "Moved/b.txt", Change("moved", "Moved/a.txt", "Moved/b.txt"))
    tasks = registry._dispatch([source, destination], time.monotonic())
    await settle()
    assert started == ["Moved/b.txt"]
    assert source in registry._pending
    release.set()
    await asyncio.gather(*tasks)


def test_move_consumption_does_not_invent_a_source_deletion():
    registry = MySpaceRegistry()
    registry._consume("alice:b.txt", 7, Change("moved", "a.txt", "b.txt"))
    assert set(registry._pending) == {("alice", "b.txt")}
    assert set(registry._events) == {("alice", "b.txt")}
