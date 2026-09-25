"""Shared human decisions; only the owning tool coroutine performs an action.

Records have an absolute deadline and a short owner lease. API workers validate
against the shared request and atomically claim one decision. The owning waiter
polls the record, so correctness does not depend on delivery of a pub/sub signal.
No event-loop object or callable crosses a process boundary.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from urllib.parse import quote

from core.infra.ephemeral import get_ephemeral_state

_PREFIX = "human-interaction:v1:"
_POLL = 0.2
_LEASE = 30


def _key(kind, chat_id, request_id=""):
    return f"{_PREFIX}{kind}:{quote(chat_id, safe='')}:{request_id}"


def _ttl(record):
    return max(1, math.ceil(record["expires_at"] - time.time()) + 60)


def _live(record):
    now = time.time()
    return record["expires_at"] > now and record["lease_until"] > now


async def register(kind, chat_id, request_id, info, timeout):
    now = time.time()
    record = dict(
        info=info, created_at=now, expires_at=now + timeout, lease_until=now + _LEASE, decision=None
    )
    await get_ephemeral_state().put(
        _key(kind, chat_id, request_id), json.dumps(record), ttl=_ttl(record)
    )


async def read(kind, chat_id, request_id):
    raw = await get_ephemeral_state().get(_key(kind, chat_id, request_id))
    return json.loads(raw) if raw else None


async def pending(kind, chat_id):
    store = get_ephemeral_state()
    result = []
    for key in await store.keys(_key(kind, chat_id)):
        raw = await store.get(key)
        if raw:
            record = json.loads(raw)
            if _live(record) and record["decision"] is None:
                result.append((record["created_at"], record["info"]))
    return [info for _, info in sorted(result, key=lambda item: item[0])]


async def chats(kind):
    from urllib.parse import unquote

    prefix = f"{_PREFIX}{kind}:"
    return sorted(
        {
            unquote(key[len(prefix) :].split(":", 1)[0])
            for key in await get_ephemeral_state().keys(prefix)
        }
    )


async def decide(kind, chat_id, request_id, decision):
    """First valid claimant wins, including answer/cancel/deadline races."""
    store = get_ephemeral_state()
    key = _key(kind, chat_id, request_id)
    while True:
        raw = await store.get(key)
        if raw is None:
            return False
        record = json.loads(raw)
        if not _live(record) or record["decision"] is not None:
            return False
        record["decision"] = decision
        if await store.compare_exchange(key, raw, json.dumps(record), ttl=_ttl(record)):
            return True


async def wait(kind, chat_id, request_id, event, timeout):
    """Return the shared decision, or None for a local resolution/deadline.

    Renew only our own pending record. A concurrent answer always wins over
    renewal. A dead worker's unanswered cards disappear after its lease ends.
    """
    store = get_ephemeral_state()
    key = _key(kind, chat_id, request_id)
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        raw = await store.get(key)
        if raw is None:
            raise RuntimeError("Human interaction state disappeared while waiting")
        record = json.loads(raw)
        if record["decision"] is not None:
            return record["decision"]
        if event.is_set():
            return None
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            # A timeout must not discard a concurrently accepted answer.
            if await store.compare_exchange(
                key,
                raw,
                json.dumps({**record, "decision": {"outcome": "timeout"}}),
                ttl=_ttl(record),
            ):
                return {"outcome": "timeout"}
            continue
        if record["lease_until"] - time.time() < _LEASE / 2:
            record["lease_until"] = time.time() + _LEASE
            await store.compare_exchange(key, raw, json.dumps(record), ttl=_ttl(record))
        # Local waiters can live on different thread-local loops. Inspect
        # the local flag only; never await another loop's asyncio.Event.
        await asyncio.sleep(min(_POLL, remaining))


async def remove(kind, chat_id, request_id):
    await get_ephemeral_state().drop(_key(kind, chat_id, request_id))
