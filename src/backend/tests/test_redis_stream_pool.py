"""Blocking chat-stream reads must not share the general request pool."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from weakref import WeakKeyDictionary


def test_blocking_pool_is_separate_and_both_are_closed(monkeypatch):
    import core.infra.redis as redis_module

    created = []

    class _Client:
        def __init__(self, max_connections) -> None:
            self.max_connections = max_connections
            self.closed = False
            created.append(self)

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        redis_module,
        "settings",
        SimpleNamespace(
            redis=SimpleNamespace(url="redis://example.invalid:6379/0", socket_timeout=30)
        ),
    )
    monkeypatch.setattr(
        redis_module.aioredis,
        "from_url",
        lambda *args, **kwargs: _Client(kwargs["max_connections"]),
    )
    monkeypatch.setattr(redis_module, "_redis_pools", WeakKeyDictionary())
    monkeypatch.setattr(redis_module, "_stream_pools", WeakKeyDictionary())

    async def _run() -> None:
        general = redis_module.get_redis()
        blocking = redis_module.get_redis(blocking=True)
        assert general is not blocking
        assert general is redis_module.get_redis()
        assert blocking is redis_module.get_redis(blocking=True)
        await redis_module.close_redis()

    asyncio.run(_run())

    assert len(created) == 2
    assert created[0].max_connections != created[1].max_connections
    assert all(client.closed for client in created)


def test_follower_asks_for_the_blocking_pool():
    import inspect

    from orchestration import chat_run_executor

    source = inspect.getsource(chat_run_executor.follow_run)
    assert "get_redis(blocking=True)" in source


def test_one_seam_redirects_both_pools(monkeypatch):
    """Patching ``get_redis`` must redirect the follower too.

    The follower and the XADD writer have to reach the same server; a second
    accessor would let a redirect move only one of them.
    """
    import core.infra.redis as redis_module

    sentinel = object()
    monkeypatch.setattr(redis_module, "_get_pooled", lambda *args, **kwargs: sentinel)
    assert redis_module.get_redis() is sentinel
    assert redis_module.get_redis(blocking=True) is sentinel
