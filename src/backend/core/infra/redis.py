"""Event-loop-local Redis clients for session storage."""

import asyncio
import threading
from typing import Optional
from weakref import WeakKeyDictionary

import redis.asyncio as aioredis

from core.config.settings import settings
from core.infra.logging import get_logger

logger = get_logger(__name__)

_redis_pools: WeakKeyDictionary[asyncio.AbstractEventLoop, aioredis.Redis] = (
    WeakKeyDictionary()
)
_redis_pools_lock = threading.Lock()
_fake_server: Optional[object] = None


def get_redis() -> aioredis.Redis:
    """Get or create the Redis client owned by the current event loop.

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
    global _fake_server
    loop = asyncio.get_running_loop()
    with _redis_pools_lock:
        existing = _redis_pools.get(loop)
        if existing is not None:
            return existing

        url = settings.redis.url
        if url.startswith("memory://"):
            import fakeredis.aioredis as _fakeredis

            if _fake_server is None:
                _fake_server = _fakeredis.FakeServer()
            client = _fakeredis.FakeRedis(
                server=_fake_server, decode_responses=True
            )
            backend = "fakeredis"
        else:
            # NOTE: redis-py 8.0 changed the default socket_timeout from None -> 5s.
            # The chat-stream follower blocks on `XREAD BLOCK 5000`; if the socket
            # timeout equals the block (5s) it fires the moment the server returns
            # its nil reply, raising a spurious "Timeout reading from redis" on
            # every idle 5s window (log spam + connection churn during long runs).
            # Pin an explicit timeout that stays well above any BLOCK we issue.
            client = aioredis.from_url(
                url,
                decode_responses=True,
                max_connections=20,
                socket_timeout=settings.redis.socket_timeout,
                socket_keepalive=True,
                health_check_interval=30,
            )
            backend = "redis"
        _redis_pools[loop] = client
        logger.info(
            "redis_pool_created",
            url=url.split("@")[-1],  # hide password
            socket_timeout=settings.redis.socket_timeout,
            backend=backend,
            event_loop=id(loop),
        )
        return client


async def close_redis() -> None:
    """Close the Redis client owned by the current event loop."""
    loop = asyncio.get_running_loop()
    with _redis_pools_lock:
        client = _redis_pools.pop(loop, None)
    if client is not None:
        await client.aclose()
        logger.info("redis_pool_closed", event_loop=id(loop))
