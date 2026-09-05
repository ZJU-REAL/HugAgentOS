"""Compatibility adapter over the durable Chat SteerQueue.

The database owns acceptance, ordering, claim leases and acknowledgement.
Redis only nudges a live worker to poll sooner; deleting or losing the Redis
key never deletes an accepted instruction.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from typing import Any, Dict, Optional

from core.db.engine import SessionLocal
from core.db.models import ChatRun
from core.infra.redis import get_redis
from core.services.steer_queue import SteerQueue, SteerQueueConflict

_STEER_KEY = "jx:chat:run:{run_id}:steer"
_STEER_TTL_SECONDS = 3600


def _key(run_id: str) -> str:
    return _STEER_KEY.format(run_id=run_id)


def _claim_owner() -> str:
    return f"steer:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def _run_identity(run_id: str) -> tuple[str, str]:
    with SessionLocal() as db:
        run = db.get(ChatRun, run_id)
        if run is None:
            raise SteerQueueConflict("target run not found")
        return str(run.chat_id), str(run.user_id)


async def _notify(run_id: str, payload: Dict[str, Any]) -> None:
    try:
        await get_redis().set(
            _key(run_id),
            json.dumps(payload, ensure_ascii=False),
            ex=_STEER_TTL_SECONDS,
        )
    except Exception:
        # Accepted is already durable. Notification loss is only latency.
        return


async def put_pending_steer(run_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Durably accept before publishing a best-effort Redis wake-up."""
    chat_id = str(payload.get("chat_id") or "")
    user_id = str(payload.get("user_id") or "")
    if not chat_id or not user_id:
        chat_id, user_id = _run_identity(run_id)
    record = SteerQueue(SessionLocal).accept(
        target_run_id=run_id,
        chat_id=chat_id,
        user_id=user_id,
        steer_id=str(payload.get("steer_id") or ""),
        message=str(payload.get("message") or ""),
        delivery_mode=str(payload.get("delivery_mode") or "steer"),
        replace_latest=bool(payload.get("replace_latest", True)),
    )
    await _notify(
        run_id,
        {
            "queue_id": record.queue_id,
            "steer_seq": record.steer_seq,
            "delivery_mode": record.delivery_mode,
        },
    )
    return record.as_payload()


async def take_pending_steer(
    run_id: str,
    *,
    owner: Optional[str] = None,
    lease_seconds: int = 90,
) -> Optional[Dict[str, Any]]:
    """Lease the oldest accepted steer; Redis payloads are only legacy input."""
    claim_owner = owner or _claim_owner()
    queue = SteerQueue(SessionLocal)
    claimed = queue.claim_next(
        run_id,
        owner=claim_owner,
        lease_seconds=lease_seconds,
    )
    if claimed is not None:
        return claimed.as_payload()

    # Upgrade an old Redis-only payload if one survived a rolling deployment.
    try:
        redis = get_redis()
        try:
            raw = await redis.getdel(_key(run_id))
        except AttributeError:  # pragma: no cover - old redis clients
            raw = await redis.get(_key(run_id))
            if raw is not None:
                await redis.delete(_key(run_id))
    except Exception:
        return None
    if not raw:
        return None
    try:
        legacy = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(legacy, dict) or not str(legacy.get("message") or "").strip():
        return None
    try:
        await put_pending_steer(
            run_id,
            {**legacy, "replace_latest": False},
        )
    except Exception:
        return None
    claimed = queue.claim_next(
        run_id,
        owner=claim_owner,
        lease_seconds=lease_seconds,
    )
    return claimed.as_payload() if claimed is not None else None


async def remove_pending_steer(run_id: str, steer_id: str) -> bool:
    """Durably cancel an accepted item; claimed/applied items cannot be retracted."""
    removed = SteerQueue(SessionLocal).cancel(run_id, steer_id)
    if removed:
        try:
            await get_redis().delete(_key(run_id))
        except Exception:
            pass
    return removed


def list_run_steers(run_id: str) -> list[Dict[str, Any]]:
    records = SteerQueue(SessionLocal).list_for_run(run_id)
    child_ids = [item.applied_run_id for item in records if item.applied_run_id]
    children: Dict[str, ChatRun] = {}
    if child_ids:
        with SessionLocal() as db:
            children = {
                str(row.run_id): row
                for row in db.query(ChatRun).filter(ChatRun.run_id.in_(child_ids)).all()
            }
    payloads: list[Dict[str, Any]] = []
    for item in records:
        payload = item.as_payload()
        # A steer normally applies inside its target run. If it arrived after
        # that run's final safe boundary, terminal commit hands it to a child
        # run instead; expose the child identity for the same durable UI
        # recovery path used by follow_up/next_run.
        if item.applied_run_id and item.applied_run_id != item.target_run_id:
            child = children.get(item.applied_run_id)
            payload.update(
                {
                    "applied_user_message_id": (
                        str(child.user_message_id)
                        if child is not None and child.user_message_id
                        else f"msg_{uuid.uuid5(uuid.NAMESPACE_URL, f'{item.queue_id}:user').hex[:16]}"
                    ),
                    "applied_run_message_id": (
                        str(child.message_id) if child is not None else None
                    ),
                    "applied_run_status": str(child.status) if child is not None else None,
                }
            )
        payloads.append(payload)
    return payloads


__all__ = [
    "list_run_steers",
    "put_pending_steer",
    "remove_pending_steer",
    "take_pending_steer",
]
