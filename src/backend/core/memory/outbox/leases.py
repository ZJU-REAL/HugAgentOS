"""Durable memory outbox: claims, lease renewal and release."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.db.models import MemoryOutbox
from sqlalchemy import and_, or_

logger = logging.getLogger(__name__)
from core.memory.outbox import common as common


def _claim_next(worker_id: str) -> Optional[str]:
    now = common._utcnow()
    lease_until = now + timedelta(seconds=max(1, common.settings.memory.outbox_lease_s))
    with common.SessionLocal() as db:
        (
            db.query(MemoryOutbox)
            .filter(
                MemoryOutbox.status == "processing",
                MemoryOutbox.lease_expires_at.isnot(None),
                MemoryOutbox.lease_expires_at <= now,
            )
            .update(
                {
                    "status": "retry",
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "next_attempt_at": now,
                    "last_error": "worker lease expired",
                    "updated_at": now,
                },
                synchronize_session=False,
            )
        )
        due = or_(MemoryOutbox.next_attempt_at.is_(None), MemoryOutbox.next_attempt_at <= now)
        query = (
            db.query(MemoryOutbox)
            .filter(
                or_(
                    MemoryOutbox.status == "pending",
                    and_(MemoryOutbox.status == "retry", due),
                )
            )
            .order_by(MemoryOutbox.created_at.asc(), MemoryOutbox.id.asc())
        )
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        row = query.first()
        if row is None:
            db.commit()
            return None
        previous_status = row.status
        affected = (
            db.query(MemoryOutbox)
            .filter(MemoryOutbox.id == row.id, MemoryOutbox.status == previous_status)
            .update(
                {
                    "status": "processing",
                    "attempts": int(row.attempts or 0) + 1,
                    "lease_owner": worker_id,
                    "lease_expires_at": lease_until,
                    "updated_at": now,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return row.id if affected else None


def _claim_specific(job_id: str, worker_id: str) -> bool:
    now = common._utcnow()
    lease_until = now + timedelta(seconds=max(1, common.settings.memory.outbox_lease_s))
    with common.SessionLocal() as db:
        affected = (
            db.query(MemoryOutbox)
            .filter(
                MemoryOutbox.id == job_id,
                or_(
                    MemoryOutbox.status == "pending",
                    and_(
                        MemoryOutbox.status == "retry",
                        or_(
                            MemoryOutbox.next_attempt_at.is_(None),
                            MemoryOutbox.next_attempt_at <= now,
                        ),
                    ),
                ),
            )
            .update(
                {
                    "status": "processing",
                    "attempts": MemoryOutbox.attempts + 1,
                    "lease_owner": worker_id,
                    "lease_expires_at": lease_until,
                    "updated_at": now,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(affected)


def get_outbox_job(job_id: str) -> Optional[dict[str, Any]]:
    with common.SessionLocal() as db:
        row = db.get(MemoryOutbox, job_id)
        if row is None:
            return None
        return {
            "id": row.id,
            "status": row.status,
            "result": row.result_json,
            "error": row.last_error,
        }


def _renew_lease(job_id: str, worker_id: str) -> bool:
    now = common._utcnow()
    lease_until = now + timedelta(seconds=max(1, common.settings.memory.outbox_lease_s))
    with common.SessionLocal() as db:
        affected = (
            db.query(MemoryOutbox)
            .filter_by(id=job_id, status="processing", lease_owner=worker_id)
            .update(
                {"lease_expires_at": lease_until, "updated_at": now},
                synchronize_session=False,
            )
        )
        db.commit()
        return bool(affected)


def _release_worker_leases(worker_id: str) -> int:
    """Make clean-shutdown work immediately recoverable by another process."""

    now = common._utcnow()
    with common.SessionLocal() as db:
        affected = (
            db.query(MemoryOutbox)
            .filter_by(status="processing", lease_owner=worker_id)
            .update(
                {
                    "status": "retry",
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "next_attempt_at": now,
                    "last_error": "worker stopped before acknowledgement",
                    "updated_at": now,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return int(affected or 0)


def _defer_claim(job_id: str, worker_id: str, retry_at: datetime) -> None:
    now = common._utcnow()
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    retry_at = max(retry_at, now + timedelta(milliseconds=100))
    with common.SessionLocal() as db:
        row = (
            db.query(MemoryOutbox)
            .filter_by(id=job_id, status="processing", lease_owner=worker_id)
            .first()
        )
        if row is None:
            return
        row.status = "retry"
        row.attempts = max(0, int(row.attempts or 1) - 1)
        row.lease_owner = None
        row.lease_expires_at = None
        row.next_attempt_at = retry_at
        row.last_error = "waiting for an older L2 effect in this scope"
        row.updated_at = now
        db.commit()


def _owns_lease(job_id: str, worker_id: str) -> bool:
    with common.SessionLocal() as db:
        return (
            db.query(MemoryOutbox.id)
            .filter_by(id=job_id, status="processing", lease_owner=worker_id)
            .first()
            is not None
        )


async def _lease_heartbeat(
    job_id: str,
    worker_id: str,
    lease_lost: asyncio.Event,
    owner_task: asyncio.Task,
) -> None:
    interval = max(0.5, common.settings.memory.outbox_lease_s / 3)
    while True:
        await asyncio.sleep(interval)
        try:
            renewed = _renew_lease(job_id, worker_id)
        except Exception:
            logger.exception("[memory_outbox] lease renewal failed id=%s", job_id)
            renewed = False
        if not renewed:
            lease_lost.set()
            owner_task.cancel()
            return
