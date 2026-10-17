"""Bound active workers and reserve overlapping paths before starting async work.

Unstarted changes remain in the durable journal and pending map. Reservations
include both ends of a move and are released even when approval is cancelled.
"""

import asyncio
import time

MAX_ACTIVE_TASKS = 16
MAX_ACTIVE_PROJECTIONS = 2


def overlaps(left, right):
    return (
        not left
        or not right
        or left == right
        or left.startswith(right + "/")
        or right.startswith(left + "/")
    )


def footprint(key, change):
    # Projection downloads use their own file signature/CAS checks. A refresh
    # must not reserve every user path while waiting for remote storage.
    if not key[1]:
        return ()
    return tuple(dict.fromkeys((key[1],) + (change.paths() if change else ())))


def move_precedes(parent, change, key, current, priorities):
    if key not in priorities:
        return True
    before = any(a.startswith(b + "/") for a in current.paths() for b in change.paths())
    after = any(b.startswith(a + "/") for a in current.paths() for b in change.paths())
    if before != after:
        return before
    # Equal endpoints or opposing ancestry are serialized in a stable order.
    return priorities[parent] < priorities[key]


def dispatch(registry, ready, now, *, force=False):
    capacity = MAX_ACTIVE_TASKS - len(registry._tasks)
    if capacity <= 0:
        return []
    moves = [(key, change) for key, change in registry._events.items() if change.kind == "moved"]
    move_priority = {key: (min(p.count("/") for p in change.paths()), key) for key, change in moves}
    ready = sorted(
        ready,
        key=lambda key: (
            key not in move_priority,
            move_priority.get(key, (key[1].count("/"), key)),
        ),
    )
    started = []
    for key in ready:
        if len(started) >= capacity:
            break
        if key in registry._inflight:
            continue
        if (
            not key[1]
            and sum(paths == ("",) for paths in registry._task_paths.values())
            >= MAX_ACTIVE_PROJECTIONS
        ):
            continue
        change = registry._events.get(key)
        paths = footprint(key, change)
        # Every move owns source and destination before synthetic source deletes
        # or stale source writes can run. Directory moves also own descendants.
        # A queued parent move owns both old and new subtrees, even while its
        # debounce/retry deadline has not arrived. Children must not run first.
        if any(
            parent != key
            and parent[0] == key[0]
            and move_precedes(parent, parent_change, key, change, move_priority)
            and any(overlaps(a, b) for a in paths for b in parent_change.paths())
            for parent, parent_change in moves
        ):
            continue
        if any(
            active[0] == key[0] and any(overlaps(a, b) for a in paths for b in reserved)
            for active, reserved in registry._active_paths.items()
        ):
            continue
        registry._pending.pop(key, None)
        registry._processing_times[key] = registry._observed.pop(key, time.time())
        registry._inflight.add(key)
        registry._active_paths[key] = paths
        versions = {key[1]: registry._versions.get(key)}
        events = {key: change} if change else {}
        task = asyncio.create_task(
            registry._guarded(key[0], [key[1]], force=force, versions=versions, events=events)
        )
        registry._tasks.add(task)
        registry._task_users[task] = key[0]
        registry._task_paths[task] = paths or ("",)

        def completed(done, key=key, observed=registry._processing_times[key]):
            # A task cancelled before its first instruction never enters the
            # guarded coroutine finally block. Release and retain its work here.
            registry._active_paths.pop(key, None)
            registry._inflight.discard(key)
            registry._processing_times.pop(key, None)
            if done.cancelled() and not registry._closed:
                registry._observed.setdefault(key, observed)
                registry._pending.setdefault(key, time.monotonic())
            registry._tasks.discard(done)
            registry._task_users.pop(done, None)
            registry._task_paths.pop(done, None)
            registry._wake.set()

        task.add_done_callback(completed)
        started.append(task)
    return started
