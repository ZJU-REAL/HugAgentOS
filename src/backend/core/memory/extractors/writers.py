"""Dispatcher that persists extraction results.

Dispatches the {ExtractorType: dict} returned by `run_extractors_with_timeout()`
to the corresponding layer:
- IDENTITY / PREFERENCE → L1 Profile (profile_memory.patch)
- FACT → L2 Milvus (core.memory.service.save_fact_entry)
- TASK → Session auxiliary layer (chats.metadata.session_memory)

Every writer must:
1. Pass through `sanitize()` — reject / redact sensitive information
2. Go through auditing
3. Respect the circuit breaker
"""

from __future__ import annotations

import logging
from typing import Optional

from core.memory.extractors.router import ExtractorType
from core.memory.audit import record as audit_record
from core.memory.audit import record_batch as audit_record_batch
from core.memory.context import MemoryContext
from core.memory.pipeline import milvus_breaker
from core.memory.sanitizer import sanitize
from core.memory.service import save_fact_entry

logger = logging.getLogger(__name__)


async def write_layered(
    results: dict[ExtractorType, Optional[dict]],
    ctx: MemoryContext,
) -> None:
    """Dispatch each extractor's output to its corresponding layer. A failure in any one writer does not affect the others."""
    identity_data = results.get(ExtractorType.IDENTITY)
    preference_data = results.get(ExtractorType.PREFERENCE)
    fact_data = results.get(ExtractorType.FACT)
    task_data = results.get(ExtractorType.TASK)

    # IDENTITY + PREFERENCE → L1 Profile
    if identity_data:
        await _write_profile_from_identity(identity_data, ctx)
    if preference_data:
        await _write_profile_from_preference(preference_data, ctx)

    # FACT → L2 Milvus
    if fact_data:
        await _write_facts_to_milvus(fact_data, ctx)

    # TASK → Session auxiliary layer
    if task_data:
        await _write_session_task(task_data, ctx)


async def _write_profile_from_identity(data: dict, ctx: MemoryContext) -> None:
    """Batch-upsert identity fields under `identity.<field>` — duplicate values short-circuit, new values overwrite old, single transaction."""
    await _upsert_profile_facts(
        data, ctx, namespace="identity",
        value_fn=lambda f: str(f.get("value", "")),
        confidentiality_aware=True,
    )


async def _write_profile_from_preference(data: dict, ctx: MemoryContext) -> None:
    """Batch-upsert preference fields under `preference.<field>`; strength is merged into value."""
    await _upsert_profile_facts(
        data, ctx, namespace="preference",
        value_fn=lambda f: f"{f.get('value', '')}（{f.get('strength', 'weak')}）",
        confidentiality_aware=False,
    )


async def _upsert_profile_facts(
    data: dict,
    ctx: MemoryContext,
    *,
    namespace: str,
    value_fn,
    confidentiality_aware: bool,
) -> None:
    """Collect (key, value, reason) from extractor facts and batch-upsert them into L1."""
    from core.memory.profile import upsert_fields

    facts = data.get("facts") or []
    fields: list[tuple[str, str, str | None]] = []
    target_ctx = ctx

    for f in facts:
        if not isinstance(f, dict):
            continue
        field = f.get("field")
        value = value_fn(f) if f else None
        if not field or not value:
            continue
        # All facts share a single ctx; if per-item confidentiality is needed, just write the first item's level separately
        if confidentiality_aware:
            target_ctx = ctx.with_confidentiality(f.get("confidentiality", "internal"))
        fields.append((f"{namespace}.{field}", value, f"extractor:{namespace}:{field}"))

    if fields:
        try:
            await upsert_fields(target_ctx, fields)
        except Exception as exc:
            logger.warning("[writer:%s] batch upsert failed (n=%d): %s",
                           namespace, len(fields), exc)


async def _write_facts_to_milvus(data: dict, ctx: MemoryContext) -> None:
    """Write FACT facts into L2 Milvus (reusing the existing mem0 Memory.add interface).

    Each item goes through sanitize individually; hitting CLASSIFIED means reject the write + audit.
    """
    if milvus_breaker.is_open():
        logger.info("[writer:fact] milvus breaker open, skipping %d facts",
                    len(data.get("facts") or []))
        return

    facts = data.get("facts") or []
    if not facts:
        return

    wrote = 0
    rejected_audit: list[dict] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        content = (f.get("content") or "").strip()
        if not content:
            continue
        san = sanitize(content)
        confidentiality = f.get("confidentiality", "internal")
        if san.reject:
            rejected_audit.append({
                "action": "write_rejected", "layer": "L2",
                "confidentiality": confidentiality,
                "reason": f"sanitizer: {','.join(san.hits)}",
            })
            continue
        try:
            await save_fact_entry(
                ctx=ctx,
                content=san.text,
                source=f.get("source", "conversation"),
                tags=f.get("tags") or [],
                confidentiality=confidentiality,
                ttl_days=int(f.get("ttl_days") or 180),
                evidence=f.get("evidence") or "",
                sanitizer_hits=san.hits,
            )
            wrote += 1
        except Exception as exc:
            logger.warning("[writer:fact] save failed: %s", exc)
            milvus_breaker.record_failure()
        else:
            milvus_breaker.record_success()

    if rejected_audit:
        await audit_record_batch(ctx, rejected_audit)
    if wrote:
        logger.info("[writer:fact] wrote %d facts to L2", wrote)


async def _write_session_task(data: dict, ctx: MemoryContext) -> None:
    """Write the session task working set into chats.metadata.session_memory.

    Does not write to Milvus / does not audit to L2; used only for this session.
    """
    task = data.get("session_task")
    if not task or not isinstance(task, dict):
        return

    if not ctx.chat_id:
        return

    try:
        from core.db.engine import SessionLocal
        from core.db.models import ChatSession
        import asyncio

        def _update():
            with SessionLocal() as session:
                row = session.query(ChatSession).filter_by(chat_id=ctx.chat_id).first()
                if not row:
                    return
                meta = dict(row.extra_data or {})
                meta["session_memory"] = task
                row.extra_data = meta
                session.commit()

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _update)
        await audit_record(ctx, action="write", layer="session", reason="session_task_update")
    except Exception as exc:
        logger.warning("[writer:task] failed: %s", exc)
