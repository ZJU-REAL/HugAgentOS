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
    """Only the tailing read may park on a connection from the stream pool."""
    import inspect

    from orchestration import run_event_stream

    backend = run_event_stream.RedisRunEventStream
    assert "get_redis(blocking=True)" in inspect.getsource(backend.wait)
    for method in (backend.append, backend.read, backend.last_write_ms):
        assert "blocking=True" not in inspect.getsource(method), method.__name__


def test_both_seams_use_redis_when_it_is_configured(monkeypatch):
    """A configured deployment keeps the Redis backends — nothing regresses."""
    import core.infra.ephemeral as ephemeral
    import orchestration.run_event_stream as res

    monkeypatch.setattr(ephemeral, "redis_configured", lambda: True)
    monkeypatch.setattr(res, "redis_configured", lambda: True)

    assert isinstance(ephemeral.get_ephemeral_state(), ephemeral.RedisEphemeralState)
    assert isinstance(res.get_run_event_stream(), res.RedisRunEventStream)


def test_cursor_grammar_is_shared_by_both_backends():
    """Callers parse the millisecond half, so both backends must mint it alike."""
    import orchestration.run_event_stream as res

    assert res.cursor_millis("1788745632008-0") == 1788745632008
    assert res.cursor_millis("nonsense") is None
    assert res.next_cursor("17-3") == "17-4"
    assert res.next_cursor("no-dash-number") == "no-dash-number"
