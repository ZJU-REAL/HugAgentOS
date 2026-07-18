"""Redis client singleton for session storage."""

from typing import Optional

import redis.asyncio as aioredis

from core.config.settings import settings
from core.infra.logging import get_logger

logger = get_logger(__name__)

_redis_pool: Optional[aioredis.Redis] = None


def get_redis() -> aioredis.Redis:
    """Get or create the shared async Redis connection pool.

    ``REDIS_URL=memory://`` (local/quick-install profile) returns an in-process
    ``fakeredis`` singleton instead of a real connection — the whole app runs in
    one process, so the chat-stream XADD writer and the follower's blocking
    ``XREAD BLOCK`` reader share the same fake server (verified: fakeredis 2.36+
    honours blocking XREAD, GETDEL, INCRBYFLOAT, pipelines, sorted sets). This is
    an **explicit** opt-in value — we never silently fall back on a connection
    error, so a mis-configured production Redis surfaces as a hard failure.
    """
    global _redis_pool
    if _redis_pool is None:
        url = settings.redis.url
        if url.startswith("memory://"):
            import fakeredis.aioredis as _fakeredis

            _redis_pool = _fakeredis.FakeRedis(decode_responses=True)
            logger.info("redis_pool_created", url="memory://", backend="fakeredis")
            return _redis_pool
        # NOTE: redis-py 8.0 changed the default socket_timeout from None → 5s.
        # The chat-stream follower blocks on `XREAD BLOCK 5000`; if the socket
        # timeout equals the block (5s) it fires the moment the server returns
        # its nil reply, raising a spurious "Timeout reading from redis" on
        # every idle 5s window (log spam + connection churn during long runs).
        # Pin an explicit timeout that stays well above any BLOCK we issue.
        _redis_pool = aioredis.from_url(
            url,
            decode_responses=True,
            max_connections=20,
            socket_timeout=settings.redis.socket_timeout,
            socket_keepalive=True,
            health_check_interval=30,
        )
        logger.info(
            "redis_pool_created",
            url=url.split("@")[-1],  # hide password
            socket_timeout=settings.redis.socket_timeout,
        )
    return _redis_pool


async def close_redis() -> None:
    """Gracefully close the Redis connection pool (call on shutdown)."""
    global _redis_pool
    if _redis_pool is not None:
        await _redis_pool.aclose()
        _redis_pool = None
        logger.info("redis_pool_closed")
