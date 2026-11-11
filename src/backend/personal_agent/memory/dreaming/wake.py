"""Wake the Dreaming loop when something changes what is due: "run now" and settings saves.

The loop runs in the Personal Agent worker, the API in another process, so the wake-up goes
through Redis: notify() pushes a token, wait() blocks on it (BLPOP, in slices below the
socket timeout). A deployment without Redis runs API and worker in one process, so an
in-process event does. Either way a lost wake-up only costs time: the loop's own sleep ends
at the next due time, at most pipeline.MAX_IDLE.
"""
import asyncio
import logging
import threading
import time

from core.config.settings import settings
from core.infra.redis import get_redis, redis_configured

KEY = "jx:pa_dreaming_wake"
log = logging.getLogger(__name__)
_lock = threading.Lock()
_waiters = set()   # (loop, asyncio.Event) of in-process waiters


def notify():
    """Called from synchronous code (routes, services) after the change is committed."""
    with _lock:
        waiters = list(_waiters)
    for loop, event in waiters:
        try: loop.call_soon_threadsafe(event.set)
        except RuntimeError: pass   # a closed loop no longer waits
    if not redis_configured(): return
    try:
        import redis
        client = redis.Redis.from_url(settings.redis.url, socket_timeout=2, socket_connect_timeout=2)
        try:
            client.rpush(KEY, "1")
            client.expire(KEY, 3600)
        finally:
            client.close()
    except Exception as exc:   # the loop still wakes at its next due time
        log.warning("Dreaming wake-up not sent (%s)", type(exc).__name__)


async def wait(seconds, *, beat=None, stopping=lambda: False):
    """Sleep up to seconds; return early on a wake-up. beat() runs between slices."""
    deadline = time.monotonic() + seconds
    event = asyncio.Event()
    entry = (asyncio.get_running_loop(), event)
    with _lock:
        _waiters.add(entry)
    try:
        while not stopping() and not event.is_set():
            left = deadline - time.monotonic()
            if left <= 0: return
            step = min(left, max(1, settings.redis.socket_timeout - 5))
            if redis_configured():
                try:
                    if await get_redis(blocking=True).blpop([KEY], timeout=max(1, int(step))):
                        await get_redis(blocking=True).delete(KEY)   # one wake-up is enough for a burst
                        return
                except Exception as exc:
                    log.warning("Dreaming wake-up wait failed (%s)", type(exc).__name__)
                    try: await asyncio.wait_for(event.wait(), step)
                    except asyncio.TimeoutError: pass
            else:
                try: await asyncio.wait_for(event.wait(), step)
                except asyncio.TimeoutError: pass
            if beat is not None: await beat()
    finally:
        with _lock:
            _waiters.discard(entry)
