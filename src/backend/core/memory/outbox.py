"""Durable post-response memory state machine.

The public scheduling boundary commits a pipeline row before it returns.  A
leased worker expands that row into idempotent per-layer candidate rows, writes
them, and finally consumes a durable settlement row.  Event-loop tasks are only
an acceleration mechanism; the database row is the source of truth.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, cast

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from core.config.settings import settings
from core.db.engine import SessionLocal
from core.db.models import MemoryOutbox
from core.memory.context import MemoryContext
from core.memory.effect_lane import EffectLaneDeferred, ordered_l2_effect
from core.memory.extractors.router import ExtractorType

logger = logging.getLogger(__name__)

_TERMINAL = {"succeeded", "quarantined"}
_LAYER_BY_EXTRACTOR = {
    ExtractorType.IDENTITY: "L1:identity",
    ExtractorType.PREFERENCE: "L1:preference",
    ExtractorType.PROCEDURAL: "L2:procedural",
    ExtractorType.GRAPH: "L3:graph",
    ExtractorType.TASK: "session:task",
}
_active_worker: Optional["MemoryOutboxWorker"] = None


class RetryableMemoryError(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _context_payload(ctx: MemoryContext) -> dict[str, Any]:
    return {
        "user_id": ctx.user_id,
        "workspace_id": ctx.workspace_id,
        "chat_id": ctx.chat_id,
        "allowed_levels": list(ctx.allowed_levels),
        "confidentiality": ctx.confidentiality,
        "actor": ctx.actor,
        "write_enabled": ctx.write_enabled,
        "scope_user_id": ctx.scope_user_id,
        "message_id": ctx.message_id,
        "effect_id": ctx.effect_id,
    }


def _context_from_payload(payload: dict[str, Any]) -> MemoryContext:
    return MemoryContext(
        user_id=str(payload.get("user_id") or ""),
        workspace_id=str(payload.get("workspace_id") or "default"),
        chat_id=payload.get("chat_id"),
        allowed_levels=tuple(
            payload.get("allowed_levels") or ("public", "internal", "sensitive")
        ),
        confidentiality=payload.get("confidentiality"),
        actor=payload.get("actor"),
        write_enabled=bool(payload.get("write_enabled")),
        scope_user_id=payload.get("scope_user_id"),
        message_id=payload.get("message_id"),
        effect_id=payload.get("effect_id"),
    )


def _effective_message_id(ctx: MemoryContext, candidate_hash: str) -> str:
    if ctx.message_id:
        return str(ctx.message_id)
    scope = ctx.chat_id or ctx.user_id or "anonymous"
    return f"legacy:{scope}:{candidate_hash[:24]}"


def _scope_key(ctx: MemoryContext) -> str:
    return _stable_hash(
        {
            "scope_user_id": ctx.effective_scope_user_id,
            "workspace_id": ctx.workspace_id or "default",
        }
    )


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
    with SessionLocal() as db:
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
            next_attempt_at=_utcnow(),
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
        "ctx": _context_payload(ctx),
        "user_message": user_message,
        "assistant_message": assistant_message,
    }
    candidate_hash = _stable_hash(payload)
    return _enqueue(
        message_id=_effective_message_id(ctx, candidate_hash),
        job_kind="pipeline",
        layer="pipeline",
        candidate_hash=candidate_hash,
        payload=payload,
        scope_key=_scope_key(ctx),
    )


def enqueue_candidate_job(
    parent_id: str,
    ctx: MemoryContext,
    extractor: ExtractorType,
    data: dict[str, Any],
) -> str:
    payload = {
        "ctx": _context_payload(ctx),
        "extractor": extractor.value,
        "data": data,
    }
    candidate_hash = _stable_hash({"extractor": extractor.value, "data": data})
    return _enqueue(
        message_id=_effective_message_id(ctx, candidate_hash),
        job_kind="candidate",
        layer=_LAYER_BY_EXTRACTOR[extractor],
        candidate_hash=candidate_hash,
        payload=payload,
        parent_id=parent_id,
        scope_key=_scope_key(ctx),
    )


def enqueue_profile_compaction(ctx: MemoryContext) -> str:
    from core.memory.profile_store import read_snapshot

    snapshot = read_snapshot(ctx.user_id, ctx.workspace_id)
    payload = {"ctx": _context_payload(ctx), "requested_revision": snapshot.revision}
    candidate_hash = _stable_hash(
        {
            "user_id": ctx.user_id,
            "workspace_id": ctx.workspace_id,
            "revision": snapshot.revision,
        }
    )
    return _enqueue(
        message_id=_effective_message_id(ctx, candidate_hash),
        job_kind="profile_compact",
        layer="L1:compact",
        candidate_hash=candidate_hash,
        payload=payload,
        scope_key=_scope_key(ctx),
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
        "ctx": _context_payload(ctx),
        "key": key,
        "text": text,
        "operation_id": operation_id,
    }
    candidate_hash = _stable_hash(
        {"operation_id": operation_id, "key": key, "text": text}
    )
    return _enqueue(
        message_id=_effective_message_id(ctx, candidate_hash),
        job_kind="profile_edit",
        layer="L1:profile_edit",
        candidate_hash=candidate_hash,
        payload=payload,
        scope_key=_scope_key(ctx),
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
        "ctx": _context_payload(ctx),
        "memory_id": memory_id,
        "text": text,
        "operation_id": operation_id,
    }
    candidate_hash = _stable_hash(
        {"operation_id": operation_id, "memory_id": memory_id, "text": text}
    )
    return _enqueue(
        message_id=_effective_message_id(ctx, candidate_hash),
        job_kind="memory_edit",
        layer="L2:memory_edit",
        candidate_hash=candidate_hash,
        payload=payload,
        scope_key=_scope_key(ctx),
    )


def _settlement_hash(message_id: str) -> str:
    return _stable_hash({"message_id": message_id, "kind": "settlement"})


def _claim_next(worker_id: str) -> Optional[str]:
    now = _utcnow()
    lease_until = now + timedelta(seconds=max(1, settings.memory.outbox_lease_s))
    with SessionLocal() as db:
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
        due = or_(
            MemoryOutbox.next_attempt_at.is_(None), MemoryOutbox.next_attempt_at <= now
        )
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
    now = _utcnow()
    lease_until = now + timedelta(seconds=max(1, settings.memory.outbox_lease_s))
    with SessionLocal() as db:
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
    with SessionLocal() as db:
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
    now = _utcnow()
    lease_until = now + timedelta(seconds=max(1, settings.memory.outbox_lease_s))
    with SessionLocal() as db:
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

    now = _utcnow()
    with SessionLocal() as db:
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
    now = _utcnow()
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    retry_at = max(retry_at, now + timedelta(milliseconds=100))
    with SessionLocal() as db:
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
    with SessionLocal() as db:
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
    interval = max(0.5, settings.memory.outbox_lease_s / 3)
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
    if not effect_rows or any(status not in _TERMINAL for (status,) in effect_rows):
        return None
    candidate_hash = _settlement_hash(message_id)
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
    now = _utcnow()
    with SessionLocal() as db:
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
                message = (
                    db.query(ChatMessage)
                    .filter(ChatMessage.message_id == message_id)
                    .first()
                )
                if message is None:
                    raise RuntimeError(
                        "assistant message is not durable yet; settlement will retry"
                    )
                if not isinstance(summary, dict):
                    raise RuntimeError(
                        "settlement result is missing its durable summary"
                    )
                extra = dict(message.extra_data or {})
                extra[METADATA_KEY] = summary
                message.extra_data = extra
            _enqueue_settlement_if_ready(db, message_id, now)
        db.commit()
    if affected:
        return message_id
    return None


def _finish_failure(job_id: str, worker_id: str, error: Exception) -> Optional[str]:
    now = _utcnow()
    with SessionLocal() as db:
        row = db.get(MemoryOutbox, job_id)
        if row is None or row.status != "processing" or row.lease_owner != worker_id:
            return None
        message_id = str(row.message_id)
        _lock_pipeline_row(db, message_id)
        quarantined = int(row.attempts or 0) >= settings.memory.outbox_max_attempts
        delay = settings.memory.outbox_retry_base_s * (
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


def _maybe_enqueue_settlement(message_id: str) -> None:
    """Compatibility/reconciliation entry point; final acks are atomic."""

    with SessionLocal() as db:
        _enqueue_settlement_if_ready(db, message_id, _utcnow())
        db.commit()


def reconcile_settlement_jobs() -> int:
    """Repair terminal historical rows that lack a settlement receipt."""

    with SessionLocal() as db:
        message_ids = [
            value
            for (value,) in db.query(MemoryOutbox.message_id)
            .filter(MemoryOutbox.job_kind == "pipeline")
            .distinct()
            .all()
        ]
    created = 0
    for message_id in message_ids:
        with SessionLocal() as db:
            existed = (
                db.query(MemoryOutbox.id)
                .filter_by(message_id=message_id, job_kind="settlement")
                .first()
                is not None
            )
            row_id = _enqueue_settlement_if_ready(db, message_id, _utcnow())
            db.commit()
            created += int(row_id is not None and not existed)
    return created


async def _extract_candidates(
    payload: dict[str, Any],
) -> dict[ExtractorType, Optional[dict]]:
    from core.memory.extractors.gate import MemoryGateUnavailable, llm_write_gate
    from core.memory.extractors.router import (
        classify_conversation,
        run_extractors_with_timeout,
    )
    from core.memory.trajectory import load_recent_trajectory

    ctx = _context_from_payload(payload["ctx"])
    user_message = str(payload.get("user_message") or "")
    assistant_message = str(payload.get("assistant_message") or "")
    classes = classify_conversation(user_message, assistant_message)
    trajectory = await load_recent_trajectory(ctx, user_message, assistant_message)
    if trajectory.verified_correction:
        classes.add(ExtractorType.PROCEDURAL)
    if not classes:
        return {}
    if settings.memory.llm_gate_enabled:
        try:
            classes = await llm_write_gate(
                user_message,
                assistant_message,
                classes,
                timeout_s=settings.memory.gate_timeout_s,
                recent_trajectory=trajectory.transcript,
                verified_correction=trajectory.verified_correction,
            )
        except MemoryGateUnavailable as exc:
            raise RetryableMemoryError(str(exc)) from exc
    if not classes:
        return {}
    return cast(
        dict[ExtractorType, dict[str, Any] | None],
        await run_extractors_with_timeout(
            classes=classes,
            user_message=user_message,
            assistant_message=assistant_message,
            ctx=ctx,
            timeout_s=settings.memory.extract_timeout_s,
            recent_trajectory=trajectory.transcript,
            verified_correction=trajectory.verified_correction,
            strict=True,
        ),
    )


async def _write_candidate(
    extractor: ExtractorType,
    data: dict[str, Any],
    ctx: MemoryContext,
) -> list[dict]:
    from core.memory.extractors.writers import write_layered

    return cast(
        list[dict[str, Any]], await write_layered({extractor: data}, ctx, strict=True)
    )


def _checkpoint_pipeline_candidates(
    row: MemoryOutbox,
    ctx: MemoryContext,
    candidates: list[dict[str, Any]],
) -> list[str]:
    """Atomically persist extraction output and every child admission."""

    now = _utcnow()
    child_ids: list[str] = []
    with SessionLocal() as db:
        pipeline = (
            db.query(MemoryOutbox)
            .filter_by(
                id=row.id,
                status="processing",
                lease_owner=row.lease_owner,
                job_kind="pipeline",
            )
            .first()
        )
        if pipeline is None:
            raise RetryableMemoryError(
                "pipeline lease was lost before candidate checkpoint"
            )
        checkpoint = dict(pipeline.result_json or {})
        durable_candidates = checkpoint.get("extracted_candidates")
        if isinstance(durable_candidates, list):
            candidates = durable_candidates

        for candidate in candidates:
            try:
                extractor = ExtractorType(str(candidate["extractor"]))
            except (KeyError, ValueError) as exc:
                raise ValueError("invalid extractor checkpoint") from exc
            data = candidate.get("data")
            if not isinstance(data, dict) or not data:
                continue
            payload = {
                "ctx": _context_payload(ctx),
                "extractor": extractor.value,
                "data": data,
            }
            candidate_hash = _stable_hash({"extractor": extractor.value, "data": data})
            existing = (
                db.query(MemoryOutbox.id)
                .filter_by(
                    message_id=pipeline.message_id,
                    layer=_LAYER_BY_EXTRACTOR[extractor],
                    candidate_hash=candidate_hash,
                )
                .first()
            )
            if existing is not None:
                child_ids.append(str(existing[0]))
                continue
            child_id = f"mout_{uuid.uuid4().hex[:24]}"
            db.add(
                MemoryOutbox(
                    id=child_id,
                    parent_id=pipeline.id,
                    message_id=pipeline.message_id,
                    scope_key=pipeline.scope_key,
                    job_kind="candidate",
                    layer=_LAYER_BY_EXTRACTOR[extractor],
                    candidate_hash=candidate_hash,
                    payload_json=payload,
                    status="pending",
                    next_attempt_at=now,
                )
            )
            child_ids.append(child_id)

        pipeline.result_json = {
            "extracted_candidates": candidates,
            "candidate_job_ids": child_ids,
        }
        pipeline.updated_at = now
        db.commit()
    return child_ids


async def _process_pipeline(row: MemoryOutbox) -> dict[str, Any]:
    ctx = _context_from_payload(dict(row.payload_json or {}).get("ctx") or {})
    checkpoint = dict(row.result_json or {})
    candidates = checkpoint.get("extracted_candidates")
    if not isinstance(candidates, list):
        results = await _extract_candidates(dict(row.payload_json or {}))
        candidates = [
            {"extractor": extractor.value, "data": data}
            for extractor, data in results.items()
            if isinstance(data, dict) and data
        ]
    child_ids = _checkpoint_pipeline_candidates(row, ctx, candidates)
    return {"candidate_job_ids": child_ids}


async def _process_candidate(row: MemoryOutbox) -> list[dict]:
    payload = dict(row.payload_json or {})
    extractor = ExtractorType(str(payload["extractor"]))
    data = payload.get("data") or {}
    ctx = replace(_context_from_payload(payload.get("ctx") or {}), effect_id=row.id)
    return await _write_candidate(extractor, data, ctx)


async def _process_profile_compact(row: MemoryOutbox) -> dict[str, bool]:
    from core.memory.profile import compact

    ctx = replace(
        _context_from_payload(dict(row.payload_json or {}).get("ctx") or {}),
        effect_id=row.id,
    )
    return {"compacted": await compact(ctx, strict=True)}


async def _process_profile_edit(row: MemoryOutbox) -> dict[str, Any]:
    from core.memory.profile import upsert_fields
    from core.memory.sanitizer import sanitize

    payload = dict(row.payload_json or {})
    ctx = replace(_context_from_payload(payload.get("ctx") or {}), effect_id=row.id)
    key = str(payload.get("key") or "")
    text = str(payload.get("text") or "")
    if not key or not text:
        raise ValueError("profile edit requires key and text")
    sanitized = sanitize(text)
    if sanitized.reject:
        raise ValueError("profile edit rejected by memory sanitizer")
    text = sanitized.text
    applied = await upsert_fields(ctx, [(key, text, "user_edit")], strict=True)
    if ctx.message_id:
        from core.evolution.settlement_store import update_entry_text

        update_entry_text(ctx.message_id, key, text)
    return {"key": key, "text": text, "applied": applied}


async def _process_memory_edit(row: MemoryOutbox) -> dict[str, Any]:
    from core.memory.service import update_memory

    payload = dict(row.payload_json or {})
    memory_id = str(payload.get("memory_id") or "")
    text = str(payload.get("text") or "")
    if not memory_id or not text:
        raise ValueError("memory edit requires memory_id and text")
    await update_memory(memory_id, text, strict=True)
    ctx = _context_from_payload(payload.get("ctx") or {})
    if ctx.message_id:
        from core.evolution.settlement_store import update_entry_text

        update_entry_text(ctx.message_id, memory_id, text)
    return {"id": memory_id, "text": text}


def _settlement_facts(message_id: str) -> tuple[list[dict], bool]:
    with SessionLocal() as db:
        rows = db.query(MemoryOutbox).filter_by(message_id=message_id).all()
        items: list[dict] = []
        failed = False
        for row in rows:
            if row.job_kind != "settlement" and row.status == "quarantined":
                failed = True
            if row.job_kind == "candidate" and isinstance(row.result_json, list):
                items.extend(item for item in row.result_json if isinstance(item, dict))
        return items, failed


def _report_settlement(
    message_id: str,
    items: Optional[list[dict]] = None,
    failed: bool = False,
) -> dict[str, Any]:
    from core.db.models import EvolutionEpisode
    from core.evolution.settlement import settle_turn
    from core.evolution.settlement_runner import acknowledge_durable_settlement

    # The Outbox is now the authoritative join. Retire the old in-process
    # watchdog before computing the final card so it cannot race the atomic
    # summary+ack transaction performed by ``_finish_success``.
    acknowledge_durable_settlement(message_id)
    with SessionLocal() as db:
        episode = (
            db.query(EvolutionEpisode.episode_id)
            .filter(EvolutionEpisode.message_id == message_id)
            .first()
        )
    summary = settle_turn(
        message_id=message_id,
        episode_id=str(episode[0]) if episode is not None else "",
        memory_entries=list(items or []),
        memory_failed=failed,
        memory_enabled=True,
    )
    return cast(dict[str, Any], summary.to_dict())


async def _process_settlement(row: MemoryOutbox) -> dict[str, Any]:
    items, failed = _settlement_facts(row.message_id)
    summary = _report_settlement(row.message_id, items=items, failed=failed)
    return {"items": items, "failed": failed, "summary": summary}


async def _process_claimed(job_id: str, worker_id: str) -> None:
    with SessionLocal() as db:
        row = db.get(MemoryOutbox, job_id)
        if row is None or row.status != "processing" or row.lease_owner != worker_id:
            return
        db.expunge(row)

    owner_task = asyncio.current_task()
    if owner_task is None:  # pragma: no cover - asyncio owns this coroutine
        return
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(
        _lease_heartbeat(job_id, worker_id, lease_lost, owner_task)
    )
    try:
        from core.memory.pipeline import get_background_semaphore

        result: Any
        async with get_background_semaphore():
            if not _owns_lease(job_id, worker_id):
                lease_lost.set()
                return
            if row.job_kind == "pipeline":
                result = await _process_pipeline(row)
            elif row.job_kind == "candidate":
                if row.layer == "L2:procedural" and row.scope_key:
                    async with ordered_l2_effect(row.scope_key, row.id):
                        result = await _process_candidate(row)
                else:
                    result = await _process_candidate(row)
            elif row.job_kind == "profile_compact":
                result = await _process_profile_compact(row)
            elif row.job_kind == "profile_edit":
                result = await _process_profile_edit(row)
            elif row.job_kind == "memory_edit":
                result = await _process_memory_edit(row)
            elif row.job_kind == "settlement":
                result = await _process_settlement(row)
            else:
                raise RuntimeError(f"unknown memory outbox job kind: {row.job_kind}")
    except asyncio.CancelledError:
        if lease_lost.is_set():
            logger.warning("[memory_outbox] lease lost; stopped job id=%s", job_id)
            return
        raise
    except EffectLaneDeferred as exc:
        _defer_claim(job_id, worker_id, exc.retry_at)
    except Exception as exc:
        logger.warning("[memory_outbox] job failed id=%s: %s", job_id, exc)
        _finish_failure(job_id, worker_id, exc)
    else:
        try:
            _finish_success(job_id, worker_id, result)
        except Exception as exc:
            # Processing can succeed while the atomic local acknowledgement
            # fails (for example the assistant message has not committed yet).
            # Roll the owned row into retry instead of parking it until lease
            # expiry; external receipts make replay safe.
            logger.warning(
                "[memory_outbox] acknowledgement failed id=%s: %s", job_id, exc
            )
            _finish_failure(job_id, worker_id, exc)
    finally:
        heartbeat.cancel()
        try:
            await heartbeat
        except asyncio.CancelledError:
            pass


async def drain_outbox(*, max_jobs: int = 100, worker_id: Optional[str] = None) -> int:
    worker = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    processed = 0
    while processed < max_jobs:
        job_id = _claim_next(worker)
        if job_id is None:
            break
        await _process_claimed(job_id, worker)
        processed += 1
    return processed


async def consume_outbox_job(job_id: str, *, wait_s: float = 5.0) -> dict[str, Any]:
    """Try an admitted interactive edit now, while leaving retry durability intact."""

    worker_id = f"api:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    if _claim_specific(job_id, worker_id):
        await _process_claimed(job_id, worker_id)
    deadline = asyncio.get_running_loop().time() + max(0.0, wait_s)
    while True:
        snapshot = get_outbox_job(job_id)
        if snapshot is None:
            raise RuntimeError(f"outbox job disappeared: {job_id}")
        if snapshot["status"] in _TERMINAL or snapshot["status"] == "retry":
            return snapshot
        if asyncio.get_running_loop().time() >= deadline:
            return snapshot
        await asyncio.sleep(0.05)


def kick_outbox_drain() -> None:
    """Wake the lifecycle-owned worker; never create an orphan consumer task."""

    worker = _active_worker
    if worker is not None:
        worker.wake()


class MemoryOutboxWorker:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    async def start(self) -> None:
        global _active_worker
        if self._task is None or self._task.done():
            self._stop.clear()
            self._wake.clear()
            reconcile_settlement_jobs()
            _active_worker = self
            self._task = asyncio.create_task(self._loop(), name="memory-outbox-worker")

    async def stop(self) -> None:
        global _active_worker
        self._stop.set()
        self._wake.set()
        if _active_worker is self:
            _active_worker = None
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        _release_worker_leases(self.worker_id)
        self._task = None

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await drain_outbox(max_jobs=100, worker_id=self.worker_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[memory_outbox] worker drain failed; retrying")
                processed = 0
            if processed:
                continue
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=settings.memory.outbox_poll_interval_s
                )
            except asyncio.TimeoutError:
                pass
            finally:
                self._wake.clear()
