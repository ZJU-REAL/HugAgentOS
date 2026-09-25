"""Short-lived device approvals. Redis CAS protects every cross-worker transition.

The browser sees only request_id; only the initiating desktop knows device_secret.
A delivery can be retried by that device until ack; ack erases the session token.
"""
import hashlib
import json
import secrets
import threading
import time

from redis.exceptions import WatchError

from core.config.settings import settings
from core.infra.redis import get_redis

TTL_SECONDS = 300
POLL_INTERVAL = 2
_PREFIX = "jx:desktop_login:"
_memory: dict[str, dict] = {}
_rates: dict[str, tuple[int, float]] = {}
_lock = threading.Lock()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _memory_mode():
    return settings.session.store_type == "memory"


async def _limit(key: str, limit: int) -> bool:
    key = _PREFIX + "rate:" + digest(key)
    if _memory_mode():
        with _lock:
            now = time.time()
            for k in list(_rates):
                if _rates[k][1] <= now:
                    del _rates[k]
            count, until = _rates.get(key, (0, now + 60))
            _rates[key] = (count + 1, until)
            return count < limit
    # Set expiry atomically: a crashed worker must not leave a permanent limit.
    return int(await get_redis().eval(
        "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n",
        1, key,
    )) <= limit


async def allow_create(address: str, secret: str) -> bool:
    # Nginx may be the peer for many users. The tighter quota is device-bound;
    # a separate generous peer cap bounds abuse without trusting arbitrary XFF.
    return await _limit("device:" + secret, 10) and await _limit("peer:" + address, 600)


async def create(device_secret: str) -> dict:
    request_id = secrets.token_urlsafe(32)
    record = {
        "status": "pending",
        "secret_hash": digest(device_secret),
        "confirm_code": secrets.token_hex(4).upper(),
        "expires_at": time.time() + TTL_SECONDS,
        "revision": 0,
    }
    if _memory_mode():
        with _lock:
            for key in list(_memory):
                if _memory[key]["expires_at"] <= time.time():
                    del _memory[key]
            _memory[request_id] = record
    else:
        await get_redis().set(_PREFIX + request_id, json.dumps(record), ex=TTL_SECONDS, nx=True)
    return {"request_id": request_id, "confirm_code": record["confirm_code"],
            "expires_in": TTL_SECONDS, "interval": POLL_INTERVAL}


async def read(request_id: str) -> dict | None:
    if _memory_mode():
        with _lock:
            record = _memory.get(request_id)
            record = dict(record) if record else None
    else:
        raw = await get_redis().get(_PREFIX + request_id)
        record = json.loads(raw) if raw else None
    return record if record and record["expires_at"] > time.time() else None


async def compare_set(request_id: str, previous: dict, updated: dict) -> bool:
    """CAS retains the original deadline, never extending an approval by polling."""
    updated = {**updated, "revision": previous["revision"] + 1}
    if _memory_mode():
        with _lock:
            current = _memory.get(request_id)
            if (not current or current["revision"] != previous["revision"]
                    or current["expires_at"] <= time.time()):
                return False
            _memory[request_id] = updated
            return True
    key = _PREFIX + request_id
    async with get_redis().pipeline(transaction=True) as pipe:
        try:
            await pipe.watch(key)
            raw = await pipe.get(key)
            current = json.loads(raw) if raw else None
            if (not current or current["revision"] != previous["revision"]
                    or current["expires_at"] <= time.time()):
                return False
            pipe.multi()
            pipe.set(key, json.dumps(updated), keepttl=True)
            await pipe.execute()
            return True
        except WatchError:
            return False
