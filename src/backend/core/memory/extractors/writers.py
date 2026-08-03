"""Dispatcher that persists extraction results.

Dispatches the {ExtractorType: dict} returned by `run_extractors_with_timeout()`
to the corresponding layer:
- IDENTITY / PREFERENCE → L1 Profile (one addressable field each)
- PROCEDURAL → L2 Milvus (core.memory.service.save_procedure_entry)
- GRAPH → L3 Neo4j (core.memory.graph.write_graph_relations)
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

from core.config.settings import settings
from core.memory.audit import record as audit_record
from core.memory.audit import record_batch as audit_record_batch
from core.memory.context import MemoryContext
from core.memory.extractors.router import ExtractorType
from core.memory.graph import parse_relation, parse_relations, write_graph_relations
from core.memory.pipeline import milvus_breaker
from core.memory.sanitizer import sanitize
from core.memory.service import (
    find_similar_procedure,
    reinforce_procedure_entry,
    save_procedure_entry,
)

logger = logging.getLogger(__name__)

# Layers a written item can belong to, as reported to the turn card.
LAYER_PROFILE = "L1"
LAYER_PROCEDURE = "L2"
LAYER_GRAPH = "L3"


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
    graph_data = results.get(ExtractorType.GRAPH)

    written: list[dict] = []

    # IDENTITY + PREFERENCE → L1 Profile
    if identity_data:
        written += await _write_profile_from_identity(identity_data, ctx)
    if preference_data:
        written += await _write_profile_from_preference(preference_data, ctx)

    # PROCEDURAL → L2 Milvus. The only thing L2 stores.
    if procedural_data:
        written += await _write_procedures_to_milvus(procedural_data, ctx)

    # GRAPH → L3 Neo4j. Declarative entity relations stay separate from the
    # procedural L2 store and therefore cannot be compiled into a skill body as
    # though a relationship were an instruction.
    if graph_data:
        written += await _write_relations_to_neo4j(graph_data, ctx)

    # TASK → Session auxiliary layer. Scoped to this conversation, so it is not
    # a long-term memory and never appears on the card.
    if task_data:
        await _write_session_task(task_data, ctx)

    return written


async def _write_relations_to_neo4j(data: dict, ctx: MemoryContext) -> list[dict]:
    """Sanitize and persist stable entity relations in L3."""
    if not settings.memory.graph_enabled:
        return []

    candidates = parse_relations(data.get("relations") or [])
    if not candidates:
        return []

    accepted = []
    rejected_audit: list[dict] = []
    for relation in candidates:
        source = sanitize(relation.source)
        target = sanitize(relation.target)
        # A redacted node has no useful identity ("[REDACTED:email] depends on
        # X"), so both hard rejects and redactions are withheld from the graph.
        hits = list(source.hits) + list(target.hits)
        if source.reject or target.reject or hits:
            rejected_audit.append(
                {
                    "action": "write_rejected",
                    "layer": LAYER_GRAPH,
                    "confidentiality": "internal",
                    "content": relation.text,
                    "reason": f"sanitizer: {','.join(hits) or 'classified'}",
                }
            )
            continue
        clean = parse_relation(
            {
                **relation.to_dict(),
                "source": source.text,
                "target": target.text,
            }
        )
        if clean is not None:
            accepted.append(clean)

    if rejected_audit:
        await audit_record_batch(ctx, rejected_audit)
    if not accepted:
        return []

    rows = await write_graph_relations(ctx, accepted)
    if not rows:
        return []

    await audit_record_batch(
        ctx,
        [
            {
                "action": "write",
                "layer": LAYER_GRAPH,
                "memory_id": row["relation_id"],
                "content": (f"{row['source']} → {row['relationship']} → {row['target']}"),
                "reason": "graph_extractor",
            }
            for row in rows
        ],
    )
    logger.info("[writer:graph] wrote or reinforced %d relations to L3", len(rows))
    return [
        {
            "layer": LAYER_GRAPH,
            "kind": "graph_relation",
            "handle": row["relation_id"],
            "text": f"{row['source']} → {row['relationship']} → {row['target']}",
            "action": "reinforce" if int(row.get("seen_count") or 1) > 1 else "write",
        }
        for row in rows
    ]


async def _write_profile_from_identity(data: dict, ctx: MemoryContext) -> list[dict]:
    """Batch-upsert identity fields under `identity.<field>` — duplicate values short-circuit, new values overwrite old, single transaction."""
    return await _upsert_profile_facts(
        data,
        ctx,
        namespace="identity",
        value_fn=lambda f: str(f.get("value", "")),
        confidentiality_aware=True,
    )


async def _write_profile_from_preference(data: dict, ctx: MemoryContext) -> list[dict]:
    """Batch-upsert preference fields under `preference.<field>`; strength is merged into value."""
    return await _upsert_profile_facts(
        data,
        ctx,
        namespace="preference",
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
        logger.warning("[writer:%s] batch upsert failed (n=%d): %s", namespace, len(fields), exc)
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

    Two gates run before anything is appended:

    1. **Near-dup check.** The same convention restated across conversations must
       not become five near-identical rows. A vector search against the scope's
       existing L2 entries runs first; a hit above the dedup threshold turns the
       write into a *reinforcement* of the existing entry (seen_count += 1,
       promoted to strong, expiry extended) instead of a new row.
    2. **Strength-tiered TTL.** A rule the extractor marks ``strong`` persists
       for the full procedure TTL. A ``weak`` rule enters *provisionally* with a
       short TTL — if it never recurs it ages out; the moment it is restated,
       gate 1 promotes it to persistent. That is "seen twice → persist" without
       needing a side table.

    Returns one item per procedure actually persisted or reinforced, carrying
    the mem0 id. A write whose id cannot be read back counts as not written: the
    card would otherwise show a memory the user cannot edit or delete, which is
    worse than not showing it.
    """
    if milvus_breaker.is_open():
        logger.info(
            "[writer:procedural] milvus breaker open, skipping %d procedures",
            len(data.get("procedures") or []),
        )
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
            rejected_audit.append(
                {
                    "action": "write_rejected",
                    "layer": "L2",
                    "confidentiality": "internal",
                    "reason": f"sanitizer: {','.join(san.hits)}",
                }
            )
            continue
        why = (item.get("why") or "")[:200]
        applies_to = (item.get("applies_to") or "")[:80]
        strength = "strong" if (item.get("strength") == "strong") else "weak"

        # Gate 1: near-dup → reinforce instead of append. A failed search
        # returns None and falls through to a normal write — a duplicate row is
        # recoverable, a dropped memory is not.
        similar = await find_similar_procedure(ctx, san.text)
        if similar:
            reinforced = await reinforce_procedure_entry(similar, strength=strength)
            if reinforced:
                written.append(
                    {
                        "layer": LAYER_PROCEDURE,
                        "kind": "procedure",
                        "handle": similar["id"],
                        "text": similar.get("memory") or san.text,
                        "why": why,
                        "applies_to": applies_to,
                        "action": "reinforce",
                    }
                )
            continue

        # Gate 2: strength decides the lifetime, not whether we listen at all.
        ttl_days = (
            settings.memory.procedure_ttl_days
            if strength == "strong"
            else settings.memory.procedure_weak_ttl_days
        )
        try:
            memory_id = await save_procedure_entry(
                ctx=ctx,
                content=san.text,
                source="procedural_extractor",
                tags=["procedure"],
                confidentiality="internal",
                ttl_days=ttl_days,
                evidence=why[:120],
                sanitizer_hits=san.hits,
                memory_meta={
                    "why": why,
                    "applies_to": applies_to,
                    "strength": strength,
                    "seen_count": 1,
                },
            )
        except Exception as exc:
            logger.warning("[writer:procedural] save failed: %s", exc)
            milvus_breaker.record_failure()
            continue

        milvus_breaker.record_success()
        if not memory_id:
            continue
        written.append(
            {
                "layer": LAYER_PROCEDURE,
                "kind": "procedure",
                "handle": memory_id,
                "text": san.text,
                "why": why,
                "applies_to": applies_to,
                "action": "write",
            }
        )

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
        import asyncio

        from core.db.engine import SessionLocal
        from core.db.models import ChatSession

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
