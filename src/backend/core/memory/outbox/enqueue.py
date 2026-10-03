"""Durable memory outbox: idempotent admission."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from core.db.models import MemoryOutbox
from core.memory.context import MemoryContext
from core.memory.extractors.router import ExtractorType
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)
from core.memory.outbox import common as common


def _enqueue(
    *,
    message_id: str,
    job_kind: str,
    layer: str,
    candidate_hash: str,
    payload: dict[str, Any],
    parent_id: Optional[str] = None,
    scope_key: Optional[str] = None,
) -> str:
    row_id = f"mout_{uuid.uuid4().hex[:24]}"
    with common.SessionLocal() as db:
        row = MemoryOutbox(
            id=row_id,
            parent_id=parent_id,
            message_id=message_id,
            scope_key=scope_key,
            job_kind=job_kind,
            layer=layer,
            candidate_hash=candidate_hash,
            payload_json=payload,
            status="pending",
            next_attempt_at=common._utcnow(),
        )
        db.add(row)
        try:
            db.commit()
            return row_id
        except IntegrityError:
            db.rollback()
            existing = (
                db.query(MemoryOutbox.id)
                .filter_by(
                    message_id=message_id,
                    layer=layer,
                    candidate_hash=candidate_hash,
                )
                .first()
            )
            if existing is None:
                raise
            return str(existing[0])


def enqueue_pipeline_job(
    ctx: MemoryContext,
    user_message: str,
    assistant_message: str,
) -> str:
    payload = {
        "ctx": common._context_payload(ctx),
        "user_message": user_message,
        "assistant_message": assistant_message,
    }
    candidate_hash = common._stable_hash(payload)
    return _enqueue(
        message_id=common._effective_message_id(ctx, candidate_hash),
        job_kind="pipeline",
        layer="pipeline",
        candidate_hash=candidate_hash,
        payload=payload,
        scope_key=common._scope_key(ctx),
    )


def enqueue_candidate_job(
    parent_id: str,
    ctx: MemoryContext,
    extractor: ExtractorType,
    data: dict[str, Any],
) -> str:
    payload = {
        "ctx": common._context_payload(ctx),
        "extractor": extractor.value,
        "data": data,
    }
    candidate_hash = common._stable_hash({"extractor": extractor.value, "data": data})
    return _enqueue(
        message_id=common._effective_message_id(ctx, candidate_hash),
        job_kind="candidate",
        layer=common._LAYER_BY_EXTRACTOR[extractor],
        candidate_hash=candidate_hash,
        payload=payload,
        parent_id=parent_id,
        scope_key=common._scope_key(ctx),
    )


def enqueue_profile_compaction(ctx: MemoryContext) -> str:
    from core.memory.profile_store import read_snapshot

    snapshot = read_snapshot(ctx.user_id, ctx.workspace_id)
    payload = {"ctx": common._context_payload(ctx), "requested_revision": snapshot.revision}
    candidate_hash = common._stable_hash(
        {
            "user_id": ctx.user_id,
            "workspace_id": ctx.workspace_id,
            "revision": snapshot.revision,
        }
    )
    return _enqueue(
        message_id=common._effective_message_id(ctx, candidate_hash),
        job_kind="profile_compact",
        layer="L1:compact",
        candidate_hash=candidate_hash,
        payload=payload,
        scope_key=common._scope_key(ctx),
    )


def enqueue_profile_edit_job(
    ctx: MemoryContext,
    key: str,
    text: str,
    *,
    operation_id: Optional[str] = None,
) -> str:
    operation_id = operation_id or f"edit_{uuid.uuid4().hex}"
    payload = {
        "ctx": common._context_payload(ctx),
        "key": key,
        "text": text,
        "operation_id": operation_id,
    }
    candidate_hash = common._stable_hash({"operation_id": operation_id, "key": key, "text": text})
    return _enqueue(
        message_id=common._effective_message_id(ctx, candidate_hash),
        job_kind="profile_edit",
        layer="L1:profile_edit",
        candidate_hash=candidate_hash,
        payload=payload,
        scope_key=common._scope_key(ctx),
    )


def enqueue_memory_edit_job(
    ctx: MemoryContext,
    memory_id: str,
    text: str,
    *,
    operation_id: Optional[str] = None,
) -> str:
    operation_id = operation_id or f"edit_{uuid.uuid4().hex}"
    payload = {
        "ctx": common._context_payload(ctx),
        "memory_id": memory_id,
        "text": text,
        "operation_id": operation_id,
    }
    candidate_hash = common._stable_hash(
        {"operation_id": operation_id, "memory_id": memory_id, "text": text}
    )
    return _enqueue(
        message_id=common._effective_message_id(ctx, candidate_hash),
        job_kind="memory_edit",
        layer="L2:memory_edit",
        candidate_hash=candidate_hash,
        payload=payload,
        scope_key=common._scope_key(ctx),
    )
