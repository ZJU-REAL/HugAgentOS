"""Event-loop-local Redis clients for session storage."""

import asyncio
import threading
from typing import Optional
from weakref import WeakKeyDictionary

import redis.asyncio as aioredis

from core.config.settings import settings
from core.infra.logging import get_logger

logger = get_logger(__name__)

# Two pools per event loop, because the two workloads have opposite shapes.
#
# General traffic is short request/response commands (session lookups on every
# authenticated request, locks, ticket stores). Chat-stream followers instead
# sit in `XREAD BLOCK 5000`, which holds its connection for the whole block and
# immediately re-issues — so one live SSE follower occupies ~one connection
# continuously. Sharing a single pool means N concurrent viewers starve login
# and every other API call, and redis-py raises `MaxConnectionsError` at once
# rather than queueing. Separate pools keep a burst of followers confined to
# their own budget.
_GENERAL_MAX_CONNECTIONS = 100
_STREAM_MAX_CONNECTIONS = 200

_redis_pools: WeakKeyDictionary[asyncio.AbstractEventLoop, aioredis.Redis] = (
    WeakKeyDictionary()
)
_stream_pools: WeakKeyDictionary[asyncio.AbstractEventLoop, aioredis.Redis] = (
    WeakKeyDictionary()
)
_redis_pools_lock = threading.Lock()
_fake_server: Optional[object] = None


def _create_client(max_connections: int) -> tuple[aioredis.Redis, str]:
    """Build one client; ``memory://`` returns a shared in-process fake."""
    global _fake_server
    url = settings.redis.url
    if url.startswith("memory://"):
        import fakeredis.aioredis as _fakeredis

        if _fake_server is None:
            _fake_server = _fakeredis.FakeServer()
        return (
            _fakeredis.FakeRedis(server=_fake_server, decode_responses=True),
            "fakeredis",
        )
    # NOTE: redis-py 8.0 changed the default socket_timeout from None -> 5s.
    # The chat-stream follower blocks on `XREAD BLOCK 5000`; if the socket
    # timeout equals the block (5s) it fires the moment the server returns
    # its nil reply, raising a spurious "Timeout reading from redis" on
    # every idle 5s window (log spam + connection churn during long runs).
    # Pin an explicit timeout that stays well above any BLOCK we issue.
    client = aioredis.from_url(
        url,
        decode_responses=True,
        max_connections=max_connections,
        socket_timeout=settings.redis.socket_timeout,
        socket_keepalive=True,
        health_check_interval=30,
    )
    return client, "redis"


def _get_pooled(
    pools: "WeakKeyDictionary[asyncio.AbstractEventLoop, aioredis.Redis]",
    *,
    max_connections: int,
    role: str,
) -> aioredis.Redis:
    loop = asyncio.get_running_loop()
    with _redis_pools_lock:
        existing = pools.get(loop)
        if existing is not None:
            return existing
        client, backend = _create_client(max_connections)
        pools[loop] = client
        logger.info(
            "redis_pool_created",
            url=settings.redis.url.split("@")[-1],  # hide password
            socket_timeout=settings.redis.socket_timeout,
            backend=backend,
            role=role,
            max_connections=max_connections,
            event_loop=id(loop),
        )
        return client


def get_redis(*, blocking: bool = False) -> aioredis.Redis:
    """Get or create the Redis client owned by the current event loop.

    ``blocking=True`` returns the pool reserved for commands that park on the
    connection (the chat-stream follower's ``XREAD BLOCK``). It stays one
    function so that redirecting Redis — in a test, or behind a different
    backend — needs exactly one seam and cannot leave the two pools pointing at
    different servers.

    redis-py async connections bind to the loop that first uses them. The
    backend also runs sub-agents in dedicated thread-local loops, so one
    process-wide client eventually raises ``Future attached to a different
    loop`` in normal API requests. Keep one pool per running loop instead.

    ``REDIS_URL=memory://`` (local/quick-install profile) returns an in-process
    ``fakeredis`` client. All loop-local clients share one fake server, so the
    chat-stream XADD writer and the follower's blocking
    ``XREAD BLOCK`` reader share the same fake server (verified: fakeredis 2.36+
    honours blocking XREAD, GETDEL, INCRBYFLOAT, pipelines, sorted sets). This is
    an **explicit** opt-in value — we never silently fall back on a connection
    error, so a mis-configured production Redis surfaces as a hard failure.
    """
    if blocking:
        return _get_pooled(
            _stream_pools, max_connections=_STREAM_MAX_CONNECTIONS, role="stream"
        )
    return _get_pooled(
        _redis_pools, max_connections=_GENERAL_MAX_CONNECTIONS, role="general"
    )


async def close_redis() -> None:
    """Close every Redis client owned by the current event loop."""
    loop = asyncio.get_running_loop()
    with _redis_pools_lock:
        clients = [
            pools.pop(loop, None) for pools in (_redis_pools, _stream_pools)
        ]
    for client in clients:
        if client is not None:
            await client.aclose()
    logger.info("redis_pool_closed", event_loop=id(loop))
