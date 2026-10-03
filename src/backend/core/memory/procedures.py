"""Idempotent procedural memory lookup, reinforcement and writes."""

from __future__ import annotations
from core.infra.time import utc_now
import asyncio
import json
import logging
from datetime import date, timedelta
from typing import Optional
from core.memory.context import Confidentiality
from core.memory.executor import run_effect

logger = logging.getLogger(__name__)
from . import backend


def _expiration_from_ttl(ttl_days: int) -> Optional[str]:
    """YYYY-MM-DD expiry for mem0's native expiration, or None for no expiry."""
    if not ttl_days or ttl_days <= 0:
        return None
    return (date.today() + timedelta(days=int(ttl_days))).isoformat()


async def find_similar_procedure(
    ctx,
    content: str,
    *,
    min_score: Optional[float] = None,
) -> Optional[dict]:
    """Search L2 for an existing procedure near-duplicate to ``content``.

    Returns the best match as ``{"id", "memory", "score", "metadata"}`` when its
    cosine score reaches ``min_score`` (default from settings), else None. Any
    failure returns None — the caller then writes a new entry, which is the
    correct degradation: a duplicate row is recoverable, a lost memory is not.
    """
    if not backend.settings.memory.enabled or not content or not ctx or not ctx.user_id:
        return None
    threshold = (
        min_score if min_score is not None else backend.settings.memory.procedure_dedup_min_score
    )

    loop = asyncio.get_running_loop()
    memory = await loop.run_in_executor(None, backend._get_memory)
    if memory is None:
        return None

    search_filters: dict = {"user_id": ctx.effective_scope_user_id}
    if ctx.workspace_id:
        search_filters["workspace_id"] = ctx.workspace_id

    try:
        result = await loop.run_in_executor(
            None,
            lambda: memory.search(content, filters=search_filters, top_k=3),
        )
    except Exception as exc:
        logger.debug("[MemoryService] dedup search failed, treating as no match: %s", exc)
        return None

    items = result.get("results", []) if isinstance(result, dict) else result
    best: Optional[dict] = None
    for m in items if isinstance(items, list) else []:
        if not isinstance(m, dict):
            continue
        meta = m.get("metadata") or {}
        if meta.get("layer") != "L2":
            continue
        score = float(m.get("score") or 0.0)
        if score < threshold:
            continue
        if best is None or score > float(best.get("score") or 0.0):
            best = {
                "id": m.get("id"),
                "memory": m.get("memory") or m.get("text") or "",
                "score": score,
                "metadata": meta,
            }
    return best if best and best.get("id") else None


async def find_procedure_by_effect_id(
    ctx,
    effect_id: str,
    *,
    strict: bool = False,
) -> Optional[dict]:
    """Find the durable receipt left by an earlier outbox attempt.

    The receipt lives with the external memory.  That closes the otherwise
    unavoidable window where mem0 accepted an add/update but the process died
    before the local outbox row could be acknowledged.
    """

    if not backend.settings.memory.enabled or not effect_id or not ctx or not ctx.user_id:
        return None
    loop = asyncio.get_running_loop()
    memory = await loop.run_in_executor(None, backend._get_memory)
    if memory is None:
        if strict:
            raise RuntimeError("memory store unavailable during idempotency lookup")
        return None
    filters: dict = {"user_id": ctx.effective_scope_user_id}
    if ctx.workspace_id:
        filters["workspace_id"] = ctx.workspace_id

    # mem0's get_all() caps the result set, so scanning it cannot prove an old
    # item receipt absent once a scope exceeds that cap. The real Milvus store
    # exposes its scalar-query client: query the scalar and array receipts
    # directly, with Strong consistency, so the result is exact and observes a
    # receipt committed immediately before a crash.
    vector_store = getattr(memory, "vector_store", None)
    client = getattr(vector_store, "client", None)
    collection_name = getattr(vector_store, "collection_name", None)
    if client is not None and collection_name:
        literal = json.dumps(effect_id, ensure_ascii=False)
        scope_parts = [f'(metadata["user_id"] == {json.dumps(ctx.effective_scope_user_id)})']
        if ctx.workspace_id:
            scope_parts.append(f'(metadata["workspace_id"] == {json.dumps(ctx.workspace_id)})')
        receipt_filter = (
            f'((metadata["outbox_effect_id"] == {literal}) or '
            f'json_contains(metadata["outbox_effect_ids"], {literal}))'
        )
        filter_expr = " and ".join([*scope_parts, receipt_filter])
        try:
            rows = await run_effect(
                client.query,
                collection_name=collection_name,
                filter=filter_expr,
                output_fields=["id", "metadata"],
                limit=5,
                consistency_level="Strong",
            )
        except Exception as exc:
            if strict:
                raise RuntimeError("procedure idempotency lookup failed") from exc
            logger.debug("[MemoryService] exact effect lookup failed: %s", exc)
            return None
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            return {
                "id": item.get("id"),
                "memory": meta.get("data") or meta.get("memory") or "",
                "metadata": meta,
            }
        return None

    try:
        exact_filters = {**filters, "outbox_effect_id": effect_id}
        exact_result = await loop.run_in_executor(
            None, lambda: memory.get_all(filters=exact_filters, top_k=5)
        )
        result = await loop.run_in_executor(
            None, lambda: memory.get_all(filters=filters, top_k=200)
        )
    except Exception as exc:
        if strict:
            raise RuntimeError("procedure idempotency lookup failed") from exc
        logger.debug("[MemoryService] effect lookup failed: %s", exc)
        return None
    exact_items = (
        exact_result.get("results", []) if isinstance(exact_result, dict) else exact_result
    )
    items = result.get("results", []) if isinstance(result, dict) else result
    combined = [
        *(exact_items if isinstance(exact_items, list) else []),
        *(items if isinstance(items, list) else []),
    ]
    for item in combined:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        receipts = meta.get("outbox_effect_ids") or []
        if meta.get("outbox_effect_id") == effect_id or effect_id in receipts:
            return {
                "id": item.get("id"),
                "memory": item.get("memory") or item.get("text") or "",
                "metadata": meta,
            }
    return None


async def reinforce_procedure_entry(
    similar: dict,
    *,
    strength: str = "weak",
    effect_id: Optional[str] = None,
    candidate_receipts: Optional[list[str]] = None,
) -> bool:
    """Record that an already-stored procedure was stated again.

    A restatement is evidence, not new content: bump ``seen_count``, promote the
    entry to strong (anything seen twice has earned persistence), and extend its
    expiry to the full procedure TTL. ``updated_at`` is bumped by mem0 itself,
    which also refreshes the retrieval-side time decay.
    """
    memory_id = similar.get("id")
    if not memory_id:
        return False

    loop = asyncio.get_running_loop()
    memory = await loop.run_in_executor(None, backend._get_memory)
    if memory is None:
        return False

    meta = similar.get("metadata") or {}
    seen = _int_or(meta.get("seen_count"), 1) + 1
    ttl = backend.settings.memory.procedure_ttl_days
    patch = {
        "seen_count": seen,
        "strength": "strong",
        "ttl_days": int(ttl),
        "last_reinforced_at": utc_now().isoformat(timespec="seconds"),
    }
    if effect_id:
        receipts = list(meta.get("outbox_effect_ids") or [])
        if meta.get("outbox_effect_id") == effect_id or effect_id in receipts:
            return True
        # Retain all receipts belonging to the currently executing candidate
        # row.  The ordered per-scope effect lane guarantees that an older row
        # has already been acknowledged before a newer row may replace this
        # small receipt set, so history does not grow without bound while a
        # crash midway through a multi-rule candidate remains replay-safe.
        candidate_id, _, _item_hash = effect_id.rpartition(":")
        receipts = [
            receipt
            for receipt in receipts
            if isinstance(receipt, str) and receipt.rpartition(":")[0] == candidate_id
        ]
        for receipt in [*(candidate_receipts or []), effect_id]:
            if (
                isinstance(receipt, str)
                and receipt.rpartition(":")[0] == candidate_id
                and receipt not in receipts
            ):
                receipts.append(receipt)
        patch["outbox_effect_id"] = effect_id
        patch["outbox_effect_ids"] = receipts
    try:
        await run_effect(
            lambda: memory.update(
                memory_id,
                metadata=patch,
                expiration_date=_expiration_from_ttl(ttl),
            ),
        )
        logger.info(
            "[MemoryService] procedure reinforced id=%s seen=%d (stated as %s)",
            memory_id,
            seen,
            strength,
        )
        return True
    except Exception as exc:
        logger.warning("[MemoryService] reinforce failed id=%s: %s", memory_id, exc)
        return False


def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


async def save_procedure_entry(
    *,
    ctx,
    content: str,
    source: str = "conversation",
    tags: Optional[list] = None,
    confidentiality: Confidentiality = "internal",
    ttl_days: int = 365,
    evidence: str = "",
    sanitizer_hits: Optional[list] = None,
    memory_meta: Optional[dict] = None,
) -> Optional[str]:
    """Write one **procedure** into L2 Milvus and return its memory id.

    L2 holds procedural knowledge only — how work is done here. It used to hold
    business facts as well, and that was the layer's central mistake: a fact is
    true at the moment it is written and decays from then on, so storing it
    means the system confidently recalls a stale number instead of looking the
    current one up. A procedure has the opposite shape — "先核验主体再取数"
    stays true, is not in any model's pretraining, and is the only kind of
    memory that can later be compiled into a skill.

    Returns the created memory id (the card needs it to offer edit and delete),
    or ``None`` when nothing was written.
    """
    if not backend.settings.memory.enabled or not content or not ctx or not ctx.user_id:
        return None

    try:
        from core.memory.pipeline import milvus_breaker
    except Exception:
        milvus_breaker = None  # type: ignore[assignment]

    loop = asyncio.get_running_loop()
    # A first _get_memory() call does mem0.Memory.from_config (~700ms); run it in the executor
    memory = await loop.run_in_executor(None, backend._get_memory)
    if memory is None:
        if milvus_breaker is not None:
            milvus_breaker.record_failure()
        return None

    metadata = {
        "layer": "L2",
        "workspace_id": ctx.workspace_id,
        "source": source,
        "tags": tags or [],
        "confidentiality": confidentiality,
        "ttl_days": int(ttl_days),
        "evidence": (evidence or "")[:120],
        "sanitizer_hits": sanitizer_hits or [],
        # Every L2 entry is procedural. The field stays because the promotion
        # chain and the retrieval filter both key on it, and because entries
        # written before this change still carry other values — they are read
        # as legacy and never written again.
        "memory_type": backend.MEMORY_TYPE_PROCEDURAL,
        # Extra structure the type carries (a procedure's reason and the task
        # family it was stated for). Kept on the memory rather than in a side
        # table so it survives the store's own merges.
        **{k: v for k, v in (memory_meta or {}).items() if v not in (None, "")},
        # The real author is written into metadata; the mem0.user_id field is already occupied
        # by the scope (under team projects it's "team:<tid>"), so author_user_id separately records "who wrote it"
        "author_user_id": ctx.user_id,
    }

    # mem0 Memory.add(messages, user_id, metadata=...) interface; content is wrapped as an assistant message
    # user_id carries the scope (shared under team projects / personal falls back to the real user)
    mem0_user_id = ctx.effective_scope_user_id
    messages = [{"role": "assistant", "content": content}]

    for attempt in range(2):
        try:
            result = await run_effect(
                # ``infer=False`` stores this text verbatim.
                #
                # By default mem0 runs its *own* LLM extraction over whatever it is
                # handed and decides for itself whether to keep anything. Our
                # procedural extractor has already done exactly that work — with a
                # prompt built for procedures rather than mem0's generic
                # fact-finding one — so leaving inference on means paying for a
                # second model call whose only power is to disagree. And it does
                # disagree: handed a distilled rule it frequently returns no
                # operations at all, which reaches us as a successful write of
                # nothing. That failure is invisible by construction — the card
                # shows no memory, the log shows no error, and the user is told the
                # turn had nothing worth remembering.
                lambda: memory.add(
                    messages,
                    user_id=mem0_user_id,
                    metadata=metadata,
                    infer=False,
                    # Native expiry: an expired entry stops being recalled without
                    # waiting for the sweeper to physically remove it. ttl_days
                    # stays in metadata as the display/extension source of truth.
                    expiration_date=_expiration_from_ttl(ttl_days),
                ),
            )
            if milvus_breaker is not None:
                milvus_breaker.record_success()
            memory_id = _added_memory_id(result)
            logger.debug(
                "[MemoryService] procedure saved user=%s ws=%s id=%s",
                ctx.user_id,
                ctx.workspace_id,
                memory_id,
            )
            return memory_id
        except Exception as exc:
            # Both a broken connection and stale credentials point at an instance
            # built from an outdated config: reset, rebuild against the current
            # config and retry once. Auth errors stay out of the Milvus breaker.
            if attempt == 0 and (backend._is_connection_error(exc) or backend._is_auth_error(exc)):
                logger.warning(
                    "[MemoryService] mem0 instance looks stale, resetting for retry: %s", exc
                )
                backend._reset_memory()
                memory = await loop.run_in_executor(None, backend._get_memory)
                if memory is not None:
                    continue
            logger.warning("[MemoryService] save_procedure_entry failed: %s", exc)
            if milvus_breaker is not None and backend._is_connection_error(exc):
                milvus_breaker.record_failure()
            return None
    return None


def _added_memory_id(result) -> Optional[str]:
    """Pull the created id out of mem0's ``add()`` return value.

    mem0 answers with ``{"results": [{"id", "memory", "event"}]}``. The id is what
    makes a written memory addressable — the card cannot offer "edit" or
    "delete" on something it cannot name — so a write whose id cannot be
    recovered is reported as not written rather than as an anonymous success.
    """
    rows = result.get("results") if isinstance(result, dict) else result
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("event") or "ADD").upper() == "DELETE":
            continue
        memory_id = row.get("id") or row.get("memory_id")
        if memory_id:
            return str(memory_id)
    return None
