"""Durable memory outbox: transactional acknowledgement and settlement."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from core.db.models import MemoryOutbox

logger = logging.getLogger(__name__)
from core.memory.outbox import common as common


def _lock_pipeline_row(db, message_id: str) -> Optional[MemoryOutbox]:
    query = (
        db.query(MemoryOutbox)
        .filter_by(message_id=message_id, job_kind="pipeline")
        .order_by(MemoryOutbox.created_at.asc(), MemoryOutbox.id.asc())
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        query = query.with_for_update()
    return query.first()


def _enqueue_settlement_if_ready(db, message_id: str, now: datetime) -> Optional[str]:
    """Insert settlement in the same transaction as the final effect ack."""

    if _lock_pipeline_row(db, message_id) is None:
        return None
    db.flush()
    effect_rows = (
        db.query(MemoryOutbox.status)
        .filter(
            MemoryOutbox.message_id == message_id,
            MemoryOutbox.job_kind != "settlement",
        )
        .all()
    )
    if not effect_rows or any(status not in common._TERMINAL for (status,) in effect_rows):
        return None
    candidate_hash = common._settlement_hash(message_id)
    existing = (
        db.query(MemoryOutbox.id)
        .filter_by(
            message_id=message_id,
            layer="settlement",
            candidate_hash=candidate_hash,
        )
        .first()
    )
    if existing is not None:
        return str(existing[0])
    row_id = f"mout_settle_{candidate_hash[:24]}"
    db.add(
        MemoryOutbox(
            id=row_id,
            message_id=message_id,
            job_kind="settlement",
            layer="settlement",
            candidate_hash=candidate_hash,
            payload_json={"message_id": message_id},
            status="pending",
            next_attempt_at=now,
        )
    )
    return row_id


def _finish_success(job_id: str, worker_id: str, result: Any) -> Optional[str]:
    now = common._utcnow()
    with common.SessionLocal() as db:
        row = db.get(MemoryOutbox, job_id)
        if row is None or row.status != "processing" or row.lease_owner != worker_id:
            return None
        message_id = str(row.message_id)
        _lock_pipeline_row(db, message_id)
        affected = (
            db.query(MemoryOutbox)
            .filter_by(id=job_id, status="processing", lease_owner=worker_id)
            .update(
                {
                    "status": "succeeded",
                    "result_json": result,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "last_error": None,
                    "completed_at": now,
                    "updated_at": now,
                },
                synchronize_session=False,
            )
        )
        if affected:
            if row.job_kind == "settlement":
                from core.db.models import ChatMessage
                from core.evolution.settlement_store import METADATA_KEY

                summary = result.get("summary") if isinstance(result, dict) else None
                message = db.query(ChatMessage).filter(ChatMessage.message_id == message_id).first()
                if message is None:
                    raise RuntimeError(
                        "assistant message is not durable yet; settlement will retry"
                    )
                if not isinstance(summary, dict):
                    raise RuntimeError("settlement result is missing its durable summary")
                extra = dict(message.extra_data or {})
                extra[METADATA_KEY] = summary
                message.extra_data = extra
            _enqueue_settlement_if_ready(db, message_id, now)
        db.commit()
    if affected:
        return message_id
    return None


def _finish_failure(job_id: str, worker_id: str, error: Exception) -> Optional[str]:
    now = common._utcnow()
    with common.SessionLocal() as db:
        row = db.get(MemoryOutbox, job_id)
        if row is None or row.status != "processing" or row.lease_owner != worker_id:
            return None
        message_id = str(row.message_id)
        _lock_pipeline_row(db, message_id)
        quarantined = int(row.attempts or 0) >= common.settings.memory.outbox_max_attempts
        delay = common.settings.memory.outbox_retry_base_s * (
            2 ** max(0, int(row.attempts or 1) - 1)
        )
        row.status = "quarantined" if quarantined else "retry"
        row.next_attempt_at = None if quarantined else now + timedelta(seconds=delay)
        row.lease_owner = None
        row.lease_expires_at = None
        row.last_error = str(error)[:2000]
        row.updated_at = now
        row.completed_at = now if quarantined else None
        if quarantined:
            _enqueue_settlement_if_ready(db, message_id, now)
        db.commit()
    return message_id


def reconcile_settlement_jobs() -> int:
    """Repair terminal historical rows that lack a settlement receipt."""

    with common.SessionLocal() as db:
        message_ids = [
            value
            for (value,) in db.query(MemoryOutbox.message_id)
            .filter(MemoryOutbox.job_kind == "pipeline")
            .distinct()
            .all()
        ]
    created = 0
    for message_id in message_ids:
        with common.SessionLocal() as db:
            existed = (
                db.query(MemoryOutbox.id)
                .filter_by(message_id=message_id, job_kind="settlement")
                .first()
                is not None
            )
            row_id = _enqueue_settlement_if_ready(db, message_id, common._utcnow())
            db.commit()
            created += int(row_id is not None and not existed)
    return created
