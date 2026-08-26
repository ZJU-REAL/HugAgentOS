"""Regression tests for Redis clients used from thread-local event loops."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from weakref import WeakKeyDictionary


def test_redis_client_is_scoped_and_closed_per_event_loop(monkeypatch):
    import core.infra.redis as redis_module

    created = []
    results = []

    class _Client:
        def __init__(self) -> None:
            self.owner = asyncio.get_running_loop()
            self.closed = False
            created.append(self)

        async def aclose(self) -> None:
            assert asyncio.get_running_loop() is self.owner
            self.closed = True

    monkeypatch.setattr(
        redis_module,
        "settings",
        SimpleNamespace(
            redis=SimpleNamespace(
                url="redis://example.invalid:6379/0", socket_timeout=30
            )
        ),
    )
    monkeypatch.setattr(
        redis_module.aioredis,
        "from_url",
        lambda *args, **kwargs: _Client(),
    )
    monkeypatch.setattr(redis_module, "_redis_pools", WeakKeyDictionary())

    def _worker() -> None:
        async def _run() -> None:
            first = redis_module.get_redis()
            second = redis_module.get_redis()
            assert first is second
            results.append(first)
            await redis_module.close_redis()

        asyncio.run(_run())

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert results[0] is not results[1]
    assert len(created) == 2
    assert all(client.closed for client in created)
