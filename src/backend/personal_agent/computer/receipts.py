"""Device receipts without screenshots in the database, and without polling for them.

A receipt's images go to the short-lived store before the receipt is written; the row
keeps only a reference, which the waiting tool call resolves once. After the row is
written the receipt route wakes that call: a Redis list push where Redis is configured
(any API process can take the receipt), an in-process signal in the desktop's
single-process backend. The waiter still reads the row itself, and reads it again on a
timeout, so a lost wake only costs latency.
"""
import asyncio
import logging
import threading
import time

from core.infra import ephemeral
from core.infra.redis import get_redis

log = logging.getLogger(__name__)
IMAGE_TTL = 600
SIGNAL_TTL = 120
IMAGE_PREFIX = "pa:command-image:"


def _signal_key(command_id):
    return "pa:command-done:" + command_id


def _image_holders(result):
    """The parts of a receipt that may carry a screenshot: a read answer, and a batch's after."""
    if not isinstance(result, dict): return []
    holders = [result]
    if isinstance(result.get("after"), dict): holders.append(result["after"])
    return holders


async def offload_images(command_id, result):
    """Return the receipt with every image moved to the short-lived store under a reference."""
    result = {**result}
    if isinstance(result.get("after"), dict): result["after"] = {**result["after"]}
    store = ephemeral.get_ephemeral_state()
    for index, holder in enumerate(_image_holders(result)):
        data = holder.pop("image_base64", None)
        if not data: continue
        key = IMAGE_PREFIX + command_id + ":" + str(index)
        await store.put(key, data, ttl=IMAGE_TTL)
        holder["image_ref"] = key
    return result


async def restore_images(result):
    """Put the images back into a receipt read from the database. A lapsed image is simply absent."""
    if not isinstance(result, dict): return result
    result = {**result}
    if isinstance(result.get("after"), dict): result["after"] = {**result["after"]}
    store = ephemeral.get_ephemeral_state()
    for holder in _image_holders(result):
        key = holder.pop("image_ref", None)
        if not isinstance(key, str) or not key.startswith(IMAGE_PREFIX): continue
        # One waiter reads each receipt once; taking the image frees the store at once.
        data = await store.take(key)
        if data: holder["image_base64"] = data
        else: holder["image_expired"] = True
    return result


class _LocalSignals:
    """One process: the receipt route and the waiting tool may run on different loops or threads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.fired = {}
        self.waiters = {}

    def fire(self, key):
        with self.lock:
            now = time.monotonic()
            self.fired = {k: at for k, at in self.fired.items() if at > now - SIGNAL_TTL}
            self.fired[key] = now
            waiters = list(self.waiters.get(key, ()))
        for loop, future in waiters:
            try: loop.call_soon_threadsafe(_wake, future)
            except RuntimeError: pass

    async def wait(self, key, timeout):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        entry = (loop, future)
        with self.lock:
            if self.fired.pop(key, None) is not None: return True
            self.waiters.setdefault(key, set()).add(entry)
        try:
            await asyncio.wait_for(future, timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            with self.lock:
                waiting = self.waiters.get(key)
                if waiting is not None:
                    waiting.discard(entry)
                    if not waiting: self.waiters.pop(key)
                self.fired.pop(key, None)


def _wake(future):
    if not future.done(): future.set_result(None)


_LOCAL = _LocalSignals()


def _in_process():
    # The same choice the short-lived store makes: no Redis means one process holds everything.
    return isinstance(ephemeral.get_ephemeral_state(), ephemeral.LocalEphemeralState)


async def notify(command_id):
    """Wake whoever waits for this command. Never fails the receipt: the waiter also re-reads the row."""
    key = _signal_key(command_id)
    if _in_process():
        _LOCAL.fire(key)
        return
    try:
        async with get_redis().pipeline(transaction=True) as pipe:
            pipe.lpush(key, "1")
            pipe.expire(key, SIGNAL_TTL)
            await pipe.execute()
    except Exception:
        log.warning("device receipt wake failed command=%s", command_id, exc_info=True)


async def wait(command_id, timeout):
    """True when woken, False on timeout. A push made before the wait began is not lost."""
    key = _signal_key(command_id)
    if _in_process(): return await _LOCAL.wait(key, timeout)
    try:
        # The blocking pool's socket timeout (30 s) stays above any wait asked for here.
        return bool(await get_redis(blocking=True).blpop([key], timeout=max(0.1, min(timeout, 10))))
    except Exception:
        log.warning("device receipt wait failed command=%s", command_id, exc_info=True)
        await asyncio.sleep(min(timeout, 0.25))
        return False
