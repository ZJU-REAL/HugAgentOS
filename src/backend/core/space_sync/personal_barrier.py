"""A read barrier drains all dependent events, rather than only the first batch."""

import asyncio
import time


async def flush(registry, owner, timeout, *, metadata_only=False, strict=True, paths=()):
    # Root refresh downloads cloud bytes; metadata reads still drain every local write.
    def relevant(key):
        if key[0] != owner or (metadata_only and not key[1]):
            return False
        if not strict and key in registry._failures:
            return False
        if not paths:
            return True
        change = registry._events.get(key)
        candidates = (key[1],) + (tuple(change.paths()) if change else ())
        return any(
            a == b or a.startswith(b + "/") or b.startswith(a + "/")
            for a in candidates
            if a
            for b in paths
        )

    deadline = time.monotonic() + timeout
    for key in list(registry._failures):
        if strict and relevant(key):
            registry._failures.pop(key, None)
    while True:
        if registry._inbox:
            await registry._inbox.flush()
        failures = [exc for key, exc in registry._failures.items() if relevant(key)]
        if failures:
            raise RuntimeError("空间文件同步未完成，本地修改已保留") from failures[0]
        for key in list(registry._deferred):
            if relevant(key):
                registry._observed.setdefault(key, registry._deferred.pop(key))
                registry._pending.setdefault(key, time.monotonic())
        keys = [
            key
            for key in list(registry._pending)
            if relevant(key) and key not in registry._inflight
        ]
        if keys:
            registry._dispatch(keys, time.monotonic(), force=True)
        waiting = [
            task
            for task in registry._tasks
            if registry._task_users.get(task) == owner
            and any(relevant((owner, p)) for p in registry._task_paths.get(task, ("",)))
        ]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("空间文件仍在同步，请稍后重试")
        if waiting:
            _, pending = await asyncio.wait(waiting, timeout=remaining)
            if pending:
                raise TimeoutError("空间文件仍在同步，请稍后重试")
            continue
        if not any(relevant(key) for key in registry._pending):
            return
        await asyncio.sleep(0.01)
