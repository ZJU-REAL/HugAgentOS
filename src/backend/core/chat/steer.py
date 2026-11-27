"""Async adapter over the durable Chat SteerQueue.

The database owns acceptance, ordering, claim leases and acknowledgement;
workers poll it at safe boundaries.
"""

from __future__ import annotations

import os
import socket
import uuid
from typing import Any, Dict, Optional

from core.db.engine import SessionLocal
from core.db.threads import run_db
from core.db.models import ChatRun
from core.harness.request_links import link_request
from core.chat.steer_queue import SteerQueue, SteerQueueConflict

def _claim_owner() -> str:
    return f"steer:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def _run_identity(run_id: str) -> tuple[str, str]:
    with SessionLocal() as db:
        run = db.get(ChatRun, run_id)
        if run is None:
            raise SteerQueueConflict("target run not found")
        return str(run.chat_id), str(run.user_id)


def _accept(run_id: str, payload: Dict[str, Any]):
    chat_id = str(payload.get("chat_id") or "")
    user_id = str(payload.get("user_id") or "")
    if not chat_id or not user_id:
        chat_id, user_id = _run_identity(run_id)
    return SteerQueue(SessionLocal).accept(
        target_run_id=run_id,
        chat_id=chat_id,
        user_id=user_id,
        steer_id=str(payload.get("steer_id") or ""),
        message=str(payload.get("message") or ""),
        delivery_mode=str(payload.get("delivery_mode") or "steer"),
        replace_latest=bool(payload.get("replace_latest", True)),
    )


def _claim_next(run_id: str, owner: str, lease_seconds: int):
    return SteerQueue(SessionLocal).claim_next(run_id, owner=owner, lease_seconds=lease_seconds)


def _claim_batch(run_id: str, owner: str, max_items: int, max_chars: int, lease_seconds: int):
    return SteerQueue(SessionLocal).claim_batch(
        run_id,
        owner=owner,
        max_items=max_items,
        max_chars=max_chars,
        lease_seconds=lease_seconds,
    )


def _has_claimable(run_id: str) -> bool:
    return SteerQueue(SessionLocal).has_claimable(run_id)


def _cancel(run_id: str, steer_id: str) -> bool:
    return SteerQueue(SessionLocal).cancel(run_id, steer_id)


async def put_pending_steer(run_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Durably accept a steer for the target run."""
    record = await run_db(_accept, run_id, payload)
    await link_request(run_id, f"steer:{record.delivery_mode}")
    return record.as_payload()


async def take_pending_steer(
    run_id: str,
    *,
    owner: Optional[str] = None,
    lease_seconds: int = 90,
) -> Optional[Dict[str, Any]]:
    """Lease the oldest accepted steer."""
    claimed = await run_db(_claim_next, run_id, owner or _claim_owner(), lease_seconds)
    return claimed.as_payload() if claimed is not None else None


async def take_pending_steers(
    run_id: str,
    *,
    owner: Optional[str] = None,
    max_items: int = 20,
    max_chars: int = 20_000,
    lease_seconds: int = 90,
) -> list[Dict[str, Any]]:
    """Lease every accepted steer for one boundary, in ``steer_seq`` order.

    All items share one claim owner, so the batch is acknowledged together.
    """
    claimed = await run_db(
        _claim_batch, run_id, owner or _claim_owner(), max_items, max_chars, lease_seconds
    )
    return [record.as_payload() for record in claimed]


async def has_pending_steer(run_id: str) -> bool:
    """Whether an accepted steer waits for this run, without leasing it."""
    return await run_db(_has_claimable, run_id)


async def remove_pending_steer(run_id: str, steer_id: str) -> bool:
    """Durably cancel an accepted item; claimed/applied items cannot be retracted."""
    return await run_db(_cancel, run_id, steer_id)


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
    "has_pending_steer",
    "list_run_steers",
    "put_pending_steer",
    "remove_pending_steer",
    "take_pending_steer",
    "take_pending_steers",
]
