"""Redis-backed pending steer instructions for live chat runs.

The API process and the worker consuming a ``ChatRun`` may be different
processes, so the hand-off cannot rely on an in-memory queue.  A run accepts at
most one pending instruction; editing/re-submitting the same queued card simply
replaces the value until the execution middleware atomically consumes it.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from core.infra.redis import get_redis
from redis.exceptions import WatchError

_STEER_KEY = "jx:chat:run:{run_id}:steer"
_STEER_TTL_SECONDS = 3600


def _key(run_id: str) -> str:
    return _STEER_KEY.format(run_id=run_id)


async def put_pending_steer(run_id: str, payload: Dict[str, Any]) -> None:
    """Create or replace the single pending steer instruction for ``run_id``."""
    await get_redis().set(
        _key(run_id),
        json.dumps(payload, ensure_ascii=False),
        ex=_STEER_TTL_SECONDS,
    )


async def take_pending_steer(run_id: str) -> Optional[Dict[str, Any]]:
    """Atomically consume the pending steer instruction, if one exists."""
    redis = get_redis()
    try:
        raw = await redis.getdel(_key(run_id))
    except AttributeError:  # pragma: no cover - compatibility with old redis clients
        raw = await redis.get(_key(run_id))
        if raw is not None:
            await redis.delete(_key(run_id))
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


async def remove_pending_steer(run_id: str, steer_id: str) -> bool:
    """Withdraw a still-pending instruction without deleting a newer one."""
    redis = get_redis()
    key = _key(run_id)
    async with redis.pipeline(transaction=True) as pipe:
        while True:
            try:
                await pipe.watch(key)
                raw = await pipe.get(key)
                if not raw:
                    return False
                try:
                    value = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    return False
                if not isinstance(value, dict) or value.get("steer_id") != steer_id:
                    return False
                pipe.multi()
                pipe.delete(key)
                result = await pipe.execute()
                return bool(result and result[0])
            except WatchError:
                # The API may have replaced the queued card between GET and
                # DELETE. Re-read instead of deleting that newer instruction.
                continue
