"""Memory retrieval, graph projection and user management operations."""

from __future__ import annotations
from core.infra.time import as_utc, utc_now
import asyncio
import logging
import math
from datetime import datetime
from typing import List, Optional
from core.memory.executor import run_effect
from core.memory.retrieval_types import MemoryRetrievalResult, RetrievedRelation, build_memory_item

logger = logging.getLogger(__name__)
from . import backend


async def retrieve_memories(
    user_id: str,
    query: str,
    limit: int = 10,
    min_score: float = 0.4,
    *,
    workspace_id: str = "default",
    allowed_levels: tuple = ("public", "internal", "sensitive"),
    timeout_s: Optional[float] = None,
) -> str:
    """Text projection of :func:`retrieve_memories_structured`.

    Kept as the historical entry point so existing callers are unaffected; the
    rendered text is byte-identical to the pre-ticket-01 implementation.  New
    callers that need memory identity (ids, layers, scores, ranks) for
    attribution should call the structured variant directly.
    """
    result = await retrieve_memories_structured(
        user_id,
        query,
        limit,
        min_score,
        workspace_id=workspace_id,
        allowed_levels=allowed_levels,
        timeout_s=timeout_s,
    )
    return result.to_text()


async def retrieve_memories_structured(
    user_id: str,
    query: str,
    limit: int = 10,
    min_score: float = 0.4,
    *,
    workspace_id: str = "default",
    allowed_levels: tuple = ("public", "internal", "sensitive"),
    timeout_s: Optional[float] = None,
) -> MemoryRetrievalResult:
    """Call mem0.Memory.search() and return typed items with their identity intact.

    Improvements:
    - widen the recall range (limit=10) before filtering
    - relevance-score threshold filtering (min_score)
    - time-decay weighting (newer memories rank higher)
    - secondary filtering by workspace_id + confidentiality (new)
    - outer timeout (new; when None, mem0's built-in behavior applies)
    - Milvus circuit breaker (new; short-circuits after consecutive failures to avoid repeated attempts)

    On failure / timeout / open breaker, degrades to an empty *result* carrying
    the reason; nothing bubbles up.  The reason matters downstream: attribution
    must be able to tell "memory had nothing relevant" apart from "memory never
    got a chance", which are different explanations for the same failed run.
    """
    if not backend.settings.memory.enabled or not user_id:
        return MemoryRetrievalResult.degraded_result("disabled")

    # The Milvus breaker and the Neo4j path are independent: an open vector
    # breaker must not hide graph relations that are still available.
    try:
        from core.memory.pipeline import milvus_breaker
    except Exception:
        milvus_breaker = None  # type: ignore[assignment]

    async def _do_search() -> MemoryRetrievalResult:
        typed_relations = await _retrieve_graph_relations(
            user_id=user_id,
            workspace_id=workspace_id,
            query=query,
            limit=5,
        )
        if milvus_breaker is not None and milvus_breaker.is_open():
            logger.info("[MemoryService] milvus breaker open, serving L3 only")
            return MemoryRetrievalResult(
                relations=typed_relations,
                degraded=True,
                degrade_reason="breaker_open",
            )

        for attempt in range(2):
            loop = asyncio.get_running_loop()
            # A cold _get_memory() start does mem0.Memory.from_config (~700ms);
            # it must run in the executor, otherwise it blocks the event loop and bypasses the wait_for budget.
            memory = await loop.run_in_executor(None, backend._get_memory)
            if memory is None:
                return MemoryRetrievalResult(
                    relations=typed_relations,
                    degraded=True,
                    degrade_reason="store_unavailable",
                )
            try:
                # mem0 2.0+: user_id must go into filters; limit was renamed top_k;
                # workspace_id also goes into filters so Milvus filters at the recall stage,
                # otherwise memories from other projects crowd out the top-K and the most relevant ones fail to be recalled.
                search_filters: dict = {"user_id": user_id}
                if workspace_id:
                    search_filters["workspace_id"] = workspace_id
                result = await loop.run_in_executor(
                    None,
                    lambda: memory.search(
                        query,
                        filters=search_filters,
                        top_k=limit,
                    ),
                )
                # mem0ai 2.x returns vector results only. L3 relations are read
                # independently from our Neo4j layer above.
                items = result.get("results", []) if isinstance(result, dict) else result

                # ── Scope + confidentiality filtering (new) ──
                filtered_items = []
                for m in items if isinstance(items, list) else []:
                    if not isinstance(m, dict):
                        continue
                    meta = m.get("metadata") or {}
                    # Legacy data without workspace_id / confidentiality passes through (backward compatible)
                    item_ws = meta.get("workspace_id")
                    if item_ws and item_ws != workspace_id:
                        continue
                    item_conf = meta.get("confidentiality")
                    if item_conf and item_conf not in allowed_levels:
                        continue

                    score = m.get("score", 1.0)
                    if score < min_score:
                        continue
                    adjusted_score = _apply_time_decay(m, score)
                    m["_adjusted_score"] = adjusted_score
                    filtered_items.append(m)

                # Evolution's decisions about this store are applied here and
                # nowhere else: a down-weighted memory ranks lower, a superseded
                # one is withheld. Both are overlay rows, so either is undone by
                # deleting a row rather than by writing back into a store that
                # has since renumbered itself.
                from core.memory.weights import apply_overlay

                filtered_items = apply_overlay(
                    filtered_items, user_id=user_id, workspace_id=workspace_id
                )
                recalled_count = len(items) if isinstance(items, list) else 0
                filtered_count = len(filtered_items)
                filtered_items = filtered_items[:5]

                # Rank is assigned after the sort+truncate so it reflects what was
                # actually injected, not the store's raw recall order.
                typed_items = []
                for index, raw in enumerate(filtered_items, start=1):
                    typed = build_memory_item(raw, rank=index, default_workspace=workspace_id)
                    if typed is not None:
                        typed_items.append(typed)

                result = MemoryRetrievalResult(
                    items=tuple(typed_items),
                    relations=typed_relations,
                    recalled_count=recalled_count,
                    filtered_count=filtered_count,
                )

                logger.info(
                    "[MemoryService] 检索: user=%s ws=%s 召回 %d → 过滤后 %d",
                    user_id,
                    workspace_id,
                    recalled_count,
                    filtered_count,
                )
                if milvus_breaker is not None:
                    milvus_breaker.record_success()

                # Record what we saw so the run can be reconstructed later even
                # after the external store rewrites its own ids.
                _record_retrieval_refs(result, user_id=user_id, workspace_id=workspace_id)
                return result
            except Exception as exc:
                if attempt == 0 and (
                    backend._is_connection_error(exc) or backend._is_auth_error(exc)
                ):
                    logger.warning("[MemoryService] Milvus 连接断开，重试: %s", exc)
                    backend._reset_memory()
                    continue
                logger.warning("[MemoryService] 检索失败，降级为空: %s", exc)
                if milvus_breaker is not None:
                    milvus_breaker.record_failure()
                return MemoryRetrievalResult(
                    relations=typed_relations,
                    degraded=True,
                    degrade_reason="search_failed",
                )
        return MemoryRetrievalResult(
            relations=typed_relations,
            degraded=True,
            degrade_reason="retry_exhausted",
        )

    if timeout_s is None:
        return await _do_search()

    try:
        return await asyncio.wait_for(_do_search(), timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.info("[MemoryService] retrieval exceeded budget %.2fs, skipping", timeout_s)
        if milvus_breaker is not None:
            milvus_breaker.record_failure()
        return MemoryRetrievalResult.degraded_result("timeout")


def _record_retrieval_refs(
    result: MemoryRetrievalResult, *, user_id: str, workspace_id: str
) -> None:
    """Best-effort shadow-mapping upsert; never affects retrieval."""
    if result.is_empty:
        return
    try:
        from core.memory.ref_shadow import record_retrieved_refs

        record_retrieved_refs(result, user_id=user_id, workspace_id=workspace_id)
    except Exception as exc:  # pragma: no cover - defensive, retrieval must not fail
        logger.debug("[MemoryService] ref shadow upsert skipped: %s", exc)


async def _retrieve_graph_relations(
    *, user_id: str, workspace_id: str, query: str, limit: int
) -> tuple[RetrievedRelation, ...]:
    """Best-effort L3 lookup, independent from the Milvus health state."""
    if not backend.settings.memory.graph_enabled:
        return ()
    try:
        from core.memory.graph import search_graph_relations

        rows = await search_graph_relations(
            user_id,
            query,
            workspace_id=workspace_id,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 - optional layer
        logger.warning("[MemoryService] graph retrieval failed: %s", exc)
        return ()
    return tuple(
        RetrievedRelation(
            source=str(row.get("source") or ""),
            relationship=str(row.get("relationship") or row.get("predicate") or ""),
            target=str(row.get("target") or ""),
            rank=index,
            workspace_id=workspace_id,
            relation_id=str(row.get("relation_id") or ""),
            predicate=str(row.get("predicate") or ""),
            confidence=_safe_float(row.get("confidence")),
        )
        for index, row in enumerate(rows, start=1)
        if isinstance(row, dict) and row.get("source") and row.get("target")
    )


def _safe_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _apply_time_decay(item: dict, base_score: float) -> float:
    """Weight newer memories higher. Half-life is roughly 70 days."""
    updated_at = item.get("updated_at") or item.get("created_at") or ""
    if not updated_at:
        return base_score

    try:
        if isinstance(updated_at, str):
            dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        elif isinstance(updated_at, datetime):
            dt = updated_at
        else:
            return base_score

        dt = as_utc(dt)
        now = utc_now()
        age_days = max(0, (now - dt).days)

        # Exponential decay: 70% base score + 30% decayed score
        decay = math.exp(-0.01 * age_days)
        return base_score * (0.7 + 0.3 * decay)
    except Exception:
        return base_score


async def get_all_memories(
    user_id: str,
    workspace_id: Optional[str] = None,
    top_k: int = 200,
) -> List[dict]:
    """Get all memory entries for a user under a given workspace (for the management API).

    - Without ``workspace_id``, no filter is pushed down, but the default ``top_k=200`` is
      already far larger than mem0's default 20, so legacy callers don't lose data to truncation
    - With ``workspace_id``, mem0 filters by metadata on the Milvus side; otherwise cross-project
      memories would squeeze out the project's own content due to top_k truncation (this was the
      root cause of the project memory panel once showing inconsistent counts)
    """
    if not backend.settings.memory.enabled or not user_id:
        return []

    filters: dict = {"user_id": user_id}
    if workspace_id:
        filters["workspace_id"] = workspace_id

    for attempt in range(2):
        memory = backend._get_memory()
        if memory is None:
            return []
        try:
            loop = asyncio.get_running_loop()
            # mem0 2.0+: user_id must go into filters; workspace_id filters as metadata
            result = await loop.run_in_executor(
                None, lambda: memory.get_all(filters=filters, top_k=top_k)
            )
            if isinstance(result, dict):
                return result.get("results", [])
            if isinstance(result, list):
                return result
            return []
        except Exception as exc:
            if attempt == 0 and (backend._is_connection_error(exc) or backend._is_auth_error(exc)):
                logger.warning("[MemoryService] Milvus 连接断开，正在重置并重试: %s", exc)
                backend._reset_memory()
                continue
            logger.warning("[MemoryService] 获取记忆列表失败: %s", exc)
            return []
    return []


async def update_memory(memory_id: str, content: str, *, strict: bool = False) -> bool:
    """Rewrite one memory's text in place, keeping its id and metadata.

    The turn card shows what was just written and lets the user correct it. That
    correction has to be an *edit*, not delete-and-rewrite: a new id would
    detach the entry from the card that produced it and from its audit trail,
    so a user fixing a typo would silently lose the memory's history.
    """
    if not memory_id or not (content or "").strip():
        if strict:
            raise ValueError("memory_id and content are required")
        return False
    for attempt in range(2):
        memory = backend._get_memory()
        if memory is None:
            if strict:
                raise RuntimeError("memory store unavailable")
            return False
        try:
            await run_effect(lambda: memory.update(memory_id, content.strip()))
            return True
        except Exception as exc:
            if attempt == 0 and (backend._is_connection_error(exc) or backend._is_auth_error(exc)):
                logger.warning("[MemoryService] Milvus 连接断开，正在重置并重试: %s", exc)
                backend._reset_memory()
                continue
            logger.warning("[MemoryService] 单条更新失败: %s", exc)
            if strict:
                raise
            return False
    return False


async def delete_memory(memory_id: str) -> bool:
    """Delete a single memory entry."""
    for attempt in range(2):
        memory = backend._get_memory()
        if memory is None:
            return False
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: memory.delete(memory_id))
            return True
        except Exception as exc:
            if attempt == 0 and (backend._is_connection_error(exc) or backend._is_auth_error(exc)):
                logger.warning("[MemoryService] Milvus 连接断开，正在重置并重试: %s", exc)
                backend._reset_memory()
                continue
            logger.warning("[MemoryService] 单条删除失败: %s", exc)
            return False
    return False


async def delete_all_memories(user_id: str) -> bool:
    """Clear all memories of a user."""
    if not backend.settings.memory.enabled or not user_id:
        return False
    for attempt in range(2):
        memory = backend._get_memory()
        if memory is None:
            return False
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: memory.delete_all(user_id=user_id))
            return True
        except Exception as exc:
            if attempt == 0 and (backend._is_connection_error(exc) or backend._is_auth_error(exc)):
                logger.warning("[MemoryService] Milvus 连接断开，正在重置并重试: %s", exc)
                backend._reset_memory()
                continue
            logger.warning("[MemoryService] 批量删除失败: %s", exc)
            return False
    return False
