"""A read barrier drains all dependent events, rather than only the first batch."""

import asyncio
import time


async def flush(registry, owner, timeout):
    deadline = time.monotonic() + timeout
    for key in list(registry._failures):
        if key[0] == owner:
            registry._failures.pop(key, None)
    while True:
        if registry._inbox:
            await registry._inbox.flush()
        failures = [exc for key, exc in registry._failures.items() if key[0] == owner]
        if failures:
            raise RuntimeError("空间文件同步未完成，本地修改已保留") from failures[0]
        for key in list(registry._deferred):
            if key[0] == owner:
                registry._observed.setdefault(key, registry._deferred.pop(key))
                registry._pending.setdefault(key, time.monotonic())
        keys = [
            key
            for key in list(registry._pending)
            if key[0] == owner and key not in registry._inflight
        ]
        if keys:
            registry._dispatch(keys, time.monotonic(), force=True)
        waiting = [task for task in registry._tasks if registry._task_users.get(task) == owner]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("空间文件仍在同步，请稍后重试")
        if waiting:
            _, pending = await asyncio.wait(waiting, timeout=remaining)
            if pending:
                raise TimeoutError("空间文件仍在同步，请稍后重试")
            continue
        if not any(key[0] == owner for key in registry._pending):
            return
        await asyncio.sleep(0.01)
