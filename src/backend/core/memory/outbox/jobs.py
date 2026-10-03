"""Durable memory outbox: per-kind memory effects and checkpointing."""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from typing import Any, Optional, cast

from core.db.models import MemoryOutbox
from core.memory.context import MemoryContext
from core.memory.extractors.router import ExtractorType

logger = logging.getLogger(__name__)
from core.memory.outbox import common as common


async def _extract_candidates(
    payload: dict[str, Any],
) -> dict[ExtractorType, Optional[dict]]:
    from core.memory.extractors.gate import MemoryGateUnavailable, llm_write_gate
    from core.memory.extractors.router import classify_conversation, run_extractors_with_timeout
    from core.memory.trajectory import load_recent_trajectory

    ctx = common._context_from_payload(payload["ctx"])
    user_message = str(payload.get("user_message") or "")
    assistant_message = str(payload.get("assistant_message") or "")
    classes = classify_conversation(user_message, assistant_message)
    trajectory = await load_recent_trajectory(ctx, user_message, assistant_message)
    if trajectory.verified_correction:
        classes.add(ExtractorType.PROCEDURAL)
    if not classes:
        return {}
    if common.settings.memory.llm_gate_enabled:
        try:
            classes = await llm_write_gate(
                user_message,
                assistant_message,
                classes,
                timeout_s=common.settings.memory.gate_timeout_s,
                recent_trajectory=trajectory.transcript,
                verified_correction=trajectory.verified_correction,
            )
        except MemoryGateUnavailable as exc:
            raise common.RetryableMemoryError(str(exc)) from exc
    if not classes:
        return {}
    return cast(
        dict[ExtractorType, dict[str, Any] | None],
        await run_extractors_with_timeout(
            classes=classes,
            user_message=user_message,
            assistant_message=assistant_message,
            ctx=ctx,
            timeout_s=common.settings.memory.extract_timeout_s,
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

    return cast(list[dict[str, Any]], await write_layered({extractor: data}, ctx, strict=True))


def _checkpoint_pipeline_candidates(
    row: MemoryOutbox,
    ctx: MemoryContext,
    candidates: list[dict[str, Any]],
) -> list[str]:
    """Atomically persist extraction output and every child admission."""

    now = common._utcnow()
    child_ids: list[str] = []
    with common.SessionLocal() as db:
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
            raise common.RetryableMemoryError("pipeline lease was lost before candidate checkpoint")
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
                "ctx": common._context_payload(ctx),
                "extractor": extractor.value,
                "data": data,
            }
            candidate_hash = common._stable_hash({"extractor": extractor.value, "data": data})
            existing = (
                db.query(MemoryOutbox.id)
                .filter_by(
                    message_id=pipeline.message_id,
                    layer=common._LAYER_BY_EXTRACTOR[extractor],
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
                    layer=common._LAYER_BY_EXTRACTOR[extractor],
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
    ctx = common._context_from_payload(dict(row.payload_json or {}).get("ctx") or {})
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
    ctx = replace(common._context_from_payload(payload.get("ctx") or {}), effect_id=row.id)
    return await _write_candidate(extractor, data, ctx)


async def _process_profile_compact(row: MemoryOutbox) -> dict[str, bool]:
    from core.memory.profile import compact

    ctx = replace(
        common._context_from_payload(dict(row.payload_json or {}).get("ctx") or {}),
        effect_id=row.id,
    )
    return {"compacted": await compact(ctx, strict=True)}


async def _process_profile_edit(row: MemoryOutbox) -> dict[str, Any]:
    from core.memory.profile import upsert_fields
    from core.memory.sanitizer import sanitize

    payload = dict(row.payload_json or {})
    ctx = replace(common._context_from_payload(payload.get("ctx") or {}), effect_id=row.id)
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
    ctx = common._context_from_payload(payload.get("ctx") or {})
    if ctx.message_id:
        from core.evolution.settlement_store import update_entry_text

        update_entry_text(ctx.message_id, memory_id, text)
    return {"id": memory_id, "text": text}


def _settlement_facts(message_id: str) -> tuple[list[dict], bool]:
    with common.SessionLocal() as db:
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
    with common.SessionLocal() as db:
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
