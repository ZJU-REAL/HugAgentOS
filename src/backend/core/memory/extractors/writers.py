"""Dispatcher that persists extraction results.

Dispatches the {ExtractorType: dict} returned by `run_extractors_with_timeout()`
to the corresponding layer:
- IDENTITY / PREFERENCE → L1 Profile (one addressable field each)
- PROCEDURAL → L2 Milvus (core.memory.service.save_procedure_entry)
- TASK → Session auxiliary layer (chats.metadata.session_memory)

Every writer must:
1. Pass through `sanitize()` — reject / redact sensitive information
2. Go through auditing
3. Respect the circuit breaker
4. **Return what it actually wrote**, item by item, with the handle needed to
   edit or delete it (a mem0 id for L2, a field key for L1). The turn card shows
   the user each memory it just wrote and offers to correct or remove it, and it
   can only do that for entries it can name. A writer reporting a bare count
   would force the card back to "wrote 3 things" — which is precisely the
   unverifiable claim this whole change exists to remove.
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
from core.memory.service import save_procedure_entry

logger = logging.getLogger(__name__)

# Layers a written item can belong to, as reported to the turn card.
LAYER_PROFILE = "L1"
LAYER_PROCEDURE = "L2"


async def write_layered(
    results: dict[ExtractorType, Optional[dict]],
    ctx: MemoryContext,
) -> list[dict]:
    """Dispatch each extractor's output to its layer and return everything written.

    A failure in any one writer does not affect the others. Each returned item is
    ``{"layer", "handle", "text", ...}`` — ``handle`` is the mem0 id for an L2
    procedure and the profile field key for an L1 entry, which is what makes the
    card's edit and delete actions possible.
    """
    identity_data = results.get(ExtractorType.IDENTITY)
    preference_data = results.get(ExtractorType.PREFERENCE)
    task_data = results.get(ExtractorType.TASK)
    procedural_data = results.get(ExtractorType.PROCEDURAL)

    written: list[dict] = []

    # IDENTITY + PREFERENCE → L1 Profile
    if identity_data:
        written += await _write_profile_from_identity(identity_data, ctx)
    if preference_data:
        written += await _write_profile_from_preference(preference_data, ctx)

    # PROCEDURAL → L2 Milvus. The only thing L2 stores.
    if procedural_data:
        written += await _write_procedures_to_milvus(procedural_data, ctx)

    # TASK → Session auxiliary layer. Scoped to this conversation, so it is not
    # a long-term memory and never appears on the card.
    if task_data:
        await _write_session_task(task_data, ctx)

    return written


async def _write_profile_from_identity(data: dict, ctx: MemoryContext) -> list[dict]:
    """Batch-upsert identity fields under `identity.<field>` — duplicate values short-circuit, new values overwrite old, single transaction."""
    return await _upsert_profile_facts(
        data, ctx, namespace="identity",
        value_fn=lambda f: str(f.get("value", "")),
        confidentiality_aware=True,
    )


async def _write_profile_from_preference(data: dict, ctx: MemoryContext) -> list[dict]:
    """Batch-upsert preference fields under `preference.<field>`; strength is merged into value."""
    return await _upsert_profile_facts(
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
) -> list[dict]:
    """Collect (key, value, reason) from extractor facts and batch-upsert them into L1.

    Returns one item per field that genuinely changed — re-stating something the
    profile already says is not a memory the card should announce.
    """
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

    if not fields:
        return []
    try:
        applied = await upsert_fields(target_ctx, fields)
    except Exception as exc:
        logger.warning("[writer:%s] batch upsert failed (n=%d): %s",
                       namespace, len(fields), exc)
        return []
    return [
        {
            "layer": LAYER_PROFILE,
            "kind": namespace,
            "handle": item["key"],
            "text": item["value"],
            "action": item["action"],
        }
        for item in applied
    ]


async def _write_procedures_to_milvus(data: dict, ctx: MemoryContext) -> list[dict]:
    """Store "how work is done here" — the whole of what L2 keeps.

    Returns one item per procedure actually persisted, carrying the mem0 id. A
    write whose id cannot be read back counts as not written: the card would
    otherwise show a memory the user cannot edit or delete, which is worse than
    not showing it.
    """
    if milvus_breaker.is_open():
        logger.info("[writer:procedural] milvus breaker open, skipping %d procedures",
                    len(data.get("procedures") or []))
        return []

    procedures = data.get("procedures") or []
    if not procedures:
        return []

    written: list[dict] = []
    rejected_audit: list[dict] = []
    for item in procedures:
        if not isinstance(item, dict):
            continue
        rule = (item.get("rule") or "").strip()
        if not rule:
            continue
        san = sanitize(rule)
        if san.reject:
            rejected_audit.append({
                "action": "write_rejected", "layer": "L2",
                "confidentiality": "internal",
                "reason": f"sanitizer: {','.join(san.hits)}",
            })
            continue
        why = (item.get("why") or "")[:200]
        applies_to = (item.get("applies_to") or "")[:80]
        try:
            memory_id = await save_procedure_entry(
                ctx=ctx,
                content=san.text,
                source="procedural_extractor",
                tags=["procedure"],
                confidentiality="internal",
                ttl_days=365,
                evidence=why[:120],
                sanitizer_hits=san.hits,
                memory_meta={
                    "why": why,
                    "applies_to": applies_to,
                    "strength": item.get("strength") or "weak",
                },
            )
        except Exception as exc:
            logger.warning("[writer:procedural] save failed: %s", exc)
            milvus_breaker.record_failure()
            continue

        milvus_breaker.record_success()
        if not memory_id:
            continue
        written.append({
            "layer": LAYER_PROCEDURE,
            "kind": "procedure",
            "handle": memory_id,
            "text": san.text,
            "why": why,
            "applies_to": applies_to,
            "action": "write",
        })

    if rejected_audit:
        await audit_record_batch(ctx, rejected_audit)
    if written:
        logger.info("[writer:procedural] wrote %d procedures to L2", len(written))
    return written


async def _write_session_task(data: dict, ctx: MemoryContext) -> int:
    """Write the session task working set into chats.metadata.session_memory.

    Does not write to Milvus / does not audit to L2; used only for this session.
    """
    task = data.get("session_task")
    if not task or not isinstance(task, dict):
        return 0

    if not ctx.chat_id:
        return 0

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
        return 0
    return 1
