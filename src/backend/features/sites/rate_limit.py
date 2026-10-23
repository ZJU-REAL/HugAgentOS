"""Shared site limits fail closed; local single-process mode keeps bounded buckets."""
import asyncio
import hashlib
import time
from fastapi import HTTPException
from redis.exceptions import RedisError
from core.config.settings import settings
from core.infra.redis import get_redis, redis_configured

_SCRIPT = "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],ARGV[1]) end; return n"

UNLOCK_LIMIT = 10
WRITE_LIMIT = 60

_rate_buckets = {}
_unlock_buckets = {}

def _count_in_window(buckets: dict[str, tuple[float, int]], key: str, window: float) -> int:
    """进程内滚动计数，返回本窗口内的第几次。"""
    now = time.monotonic()
    start, count = buckets.get(key, (now, 0))
    if now - start >= window:
        start, count = now, 0
    count += 1
    buckets[key] = (start, count)
    if len(buckets) > 10000:  # guard against memory bloat
        stale = [k for k, (begun, _) in buckets.items() if now - begun >= window]
        for k in stale:
            del buckets[k]
        if len(buckets) > 10000:  # 全在窗口内，只能整桶丢
            buckets.clear()
    return count


async def count_attempt(ip, slug, *, unlock=False):
    window, maximum = (300, UNLOCK_LIMIT) if unlock else (60, WRITE_LIMIT)
    if settings.deploy.is_local and not redis_configured():
        count = _count_in_window(_unlock_buckets if unlock else _rate_buckets, f"{ip}|{slug}", window)
    else:
        if not redis_configured():
            raise HTTPException(503, "Shared site rate limiter is not configured")
        key = "site-limit:" + ("unlock:" if unlock else "write:") + hashlib.sha256(f"{ip}|{slug}".encode()).hexdigest()
        try:
            count = await asyncio.wait_for(get_redis().eval(_SCRIPT, 1, key, window), 3)
        except (RedisError, asyncio.TimeoutError):
            raise HTTPException(503, "Shared site rate limiter is unavailable")
    if count > maximum:
        raise HTTPException(429, "Too many attempts; retry later", headers={"Retry-After": str(window)})
