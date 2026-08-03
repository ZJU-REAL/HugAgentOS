"""mem0 memory service wrapper

Directly reuses mem0 framework capabilities:
- vector retrieval (Milvus)
- graph retrieval (Neo4j, enable_graph=True)
- native expiration (add/update ``expiration_date``; expired entries are hidden
  from search and get_all)

What it deliberately does NOT delegate to mem0: extraction and dedup. Writes go
in with ``infer=False`` after our own extractors have judged the turn, and
near-duplicate detection is our own pre-write search (`find_similar_procedure` +
`reinforce_procedure_entry`) — mem0's dedup lives inside the ``infer`` pass we
skip, so skipping it without a replacement made every L2 write a blind append.

This file only does config assembly + async wrapping beyond that.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from datetime import date, datetime, timedelta
from typing import List, Optional

from core.config.settings import settings
from core.memory.context import Confidentiality
from core.memory.retrieval_types import (
    MemoryRetrievalResult,
    RetrievedRelation,
    build_memory_item,
)

logger = logging.getLogger(__name__)


# Single source of truth for the Milvus collection mem0 reads/writes.
# scripts/migrate_memory_isolation.py imports this so the one-off migration can
# never drift from the runtime collection name.
MEMORY_COLLECTION_NAME = "hugagent_memories"

# The only kind of memory L2 stores. Facts used to live here too; they no longer
# do, because a stored fact is a snapshot that starts decaying the moment it is
# written, while a procedure — how work is done here — stays true and is the
# only memory a skill can be compiled from.
MEMORY_TYPE_PROCEDURAL = "procedural"


# Thread-safe singleton; failures are not cached (retries allowed)
_memory_instance = None
_memory_lock = threading.Lock()
_memory_init_failed = False
_embedding_patched = False


def _patch_mem0_embedding() -> None:
    """Patch mem0 OpenAIEmbedding to remove dimensions param.

    qwen3_embedding_8b does not support the matryoshka dimensions parameter,
    but mem0's OpenAIEmbedding.embed() hardcodes dimensions=...
    Patched only once.
    """
    global _embedding_patched
    if _embedding_patched:
        return
    try:
        from mem0.embeddings.openai import OpenAIEmbedding as _OAIEmbed

        def _patched_embed(self, text, memory_action=None):
            text = text.replace("\n", " ")
            return (
                self.client.embeddings.create(input=[text], model=self.config.model)
                .data[0]
                .embedding
            )

        _OAIEmbed.embed = _patched_embed
        _embedding_patched = True
    except Exception:
        pass


def _build_mem0_config() -> dict:
    """
    Assemble the mem0 config:
    - LLM: from DB (memory role) or env fallback
    - Embedder: from DB (embedding role) or env fallback
    - Vector Store: Milvus
    - Graph Store: Neo4j (optional, controlled by settings.memory.graph_enabled)
    """
    # Resolve LLM config from DB
    try:
        from core.services.model_config import ModelConfigService
        svc = ModelConfigService.get_instance()
        mem_cfg = svc.resolve("memory")
        embed_cfg = svc.resolve("embedding")
    except Exception:
        mem_cfg = None
        embed_cfg = None

    llm_model = mem_cfg.model_name if mem_cfg else settings.memory.model_name
    llm_url = mem_cfg.base_url if mem_cfg else settings.memory.model_url
    llm_key = mem_cfg.api_key if mem_cfg else settings.memory.api_key

    embed_model = embed_cfg.model_name if embed_cfg else settings.memory.embed_model
    embed_url = embed_cfg.base_url if embed_cfg else settings.memory.embed_url
    embed_key = embed_cfg.api_key if embed_cfg else settings.memory.embed_api_key
    embed_dims = int((embed_cfg.extra.get("dimensions") if embed_cfg else None) or settings.memory.embed_dims)

    config: dict = {
        "llm": {
            "provider": "openai",
            "config": {
                "model": llm_model,
                "openai_base_url": llm_url,
                "api_key": llm_key,
                "temperature": 0.1,
                "max_tokens": 2000,
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": embed_model,
                "openai_base_url": embed_url,
                "api_key": embed_key,
            },
        },
        "vector_store": {
            "provider": "milvus",
            "config": {
                "url": settings.memory.milvus_url,
                "token": settings.memory.milvus_token,
                "collection_name": MEMORY_COLLECTION_NAME,
                "embedding_model_dims": embed_dims,
                # The default L2 metric + mem0 treating distance as score would rank "less
                # similar first". qwen3_embedding_8b outputs L2-normalized vectors, so COSINE
                # is equivalent to IP; COSINE is the more intuitive choice. Existing
                # collections' indexes need a one-off migration via rebuild_index_metric.py.
                "metric_type": "COSINE",
            },
        },
        "version": "v1.1",
        # No ``custom_fact_extraction_prompt``. It configured mem0's own
        # LLM pass over whatever it is handed — a *fact* extractor, which is
        # precisely the thing L2 no longer stores. Every write now goes in with
        # ``infer=False`` after our procedural extractor has done the judging,
        # so this prompt could only ever run as a second opinion nobody asked
        # for. Leaving it configured would suggest a code path that no longer
        # exists.
    }

    # Graph memory (optional)
    if settings.memory.graph_enabled:
        config["graph_store"] = {
            "provider": "neo4j",
            "config": {
                "url": settings.memory.neo4j_url,
                "username": settings.memory.neo4j_username,
                "password": settings.memory.neo4j_password,
            },
        }

    return config


def _get_memory() -> Optional[object]:
    """Thread-safe lazy initialization: cache the instance on success; on failure allow retry next time."""
    global _memory_instance, _memory_init_failed

    if not settings.memory.enabled:
        return None

    # Fast path: already initialized
    if _memory_instance is not None:
        return _memory_instance

    with _memory_lock:
        # Double-check after acquiring lock
        if _memory_instance is not None:
            return _memory_instance

        try:
            _patch_mem0_embedding()
            from mem0 import Memory
            cfg = _build_mem0_config()
            logger.info("[MemoryService] 初始化 mem0.Memory (graph=%s)", settings.memory.graph_enabled)
            _memory_instance = Memory.from_config(cfg)
            _memory_init_failed = False
            return _memory_instance
        except Exception as exc:
            _memory_init_failed = True
            logger.error("[MemoryService] 初始化失败，记忆功能将降级为空: %s", exc)
            return None


def _reset_memory() -> None:
    """Reset the cached memory instance so that the next call to _get_memory() reinitializes it.

    Used when the Milvus connection is broken (e.g. closed channel).
    """
    global _memory_instance, _memory_init_failed
    with _memory_lock:
        _memory_instance = None
        _memory_init_failed = False
    logger.info("[MemoryService] 已重置 mem0 实例，下次调用将重新初始化")


def _is_connection_error(exc: Exception) -> bool:
    """Check if an exception indicates a broken Milvus/gRPC connection."""
    msg = str(exc).lower()
    return any(kw in msg for kw in ("closed channel", "connection refused", "unavailable", "grpc"))


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
    if not settings.memory.enabled or not user_id:
        return MemoryRetrievalResult.degraded_result("disabled")

    # Breaker short-circuit: skip the attempt if Milvus has failed consecutively recently
    try:
        from core.memory.pipeline import milvus_breaker
        if milvus_breaker.is_open():
            logger.info("[MemoryService] milvus breaker open, skipping retrieval")
            return MemoryRetrievalResult.degraded_result("breaker_open")
    except Exception:
        milvus_breaker = None  # type: ignore[assignment]

    async def _do_search() -> MemoryRetrievalResult:
        for attempt in range(2):
            loop = asyncio.get_running_loop()
            # A cold _get_memory() start does mem0.Memory.from_config (~700ms);
            # it must run in the executor, otherwise it blocks the event loop and bypasses the wait_for budget.
            memory = await loop.run_in_executor(None, _get_memory)
            if memory is None:
                return MemoryRetrievalResult.degraded_result("store_unavailable")
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
                    )
                )
                # mem0 v1.1 returns {"results": [...], "relations": [...]}
                items = result.get("results", []) if isinstance(result, dict) else result
                relations = result.get("relations", []) if isinstance(result, dict) else []

                # ── Scope + confidentiality filtering (new) ──
                filtered_items = []
                for m in (items if isinstance(items, list) else []):
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
                    typed = build_memory_item(
                        raw, rank=index, default_workspace=workspace_id
                    )
                    if typed is not None:
                        typed_items.append(typed)

                typed_relations = []
                for index, r in enumerate(relations[:5], start=1):
                    if not isinstance(r, dict):
                        continue
                    typed_relations.append(
                        RetrievedRelation(
                            source=r.get("source", ""),
                            relationship=r.get("relationship", ""),
                            target=r.get("target", ""),
                            rank=index,
                        )
                    )

                result = MemoryRetrievalResult(
                    items=tuple(typed_items),
                    relations=tuple(typed_relations),
                    recalled_count=recalled_count,
                    filtered_count=filtered_count,
                )

                logger.info(
                    "[MemoryService] 检索: user=%s ws=%s 召回 %d → 过滤后 %d",
                    user_id, workspace_id, recalled_count, filtered_count,
                )
                if milvus_breaker is not None:
                    milvus_breaker.record_success()

                # Record what we saw so the run can be reconstructed later even
                # after the external store rewrites its own ids.
                _record_retrieval_refs(result, user_id=user_id, workspace_id=workspace_id)
                return result
            except Exception as exc:
                if attempt == 0 and _is_connection_error(exc):
                    logger.warning("[MemoryService] Milvus 连接断开，重试: %s", exc)
                    _reset_memory()
                    continue
                logger.warning("[MemoryService] 检索失败，降级为空: %s", exc)
                if milvus_breaker is not None:
                    milvus_breaker.record_failure()
                return MemoryRetrievalResult.degraded_result("search_failed")
        return MemoryRetrievalResult.degraded_result("retry_exhausted")

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
    if not settings.memory.enabled or not content or not ctx or not ctx.user_id:
        return None
    threshold = (
        min_score if min_score is not None
        else settings.memory.procedure_dedup_min_score
    )

    loop = asyncio.get_running_loop()
    memory = await loop.run_in_executor(None, _get_memory)
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


async def reinforce_procedure_entry(similar: dict, *, strength: str = "weak") -> bool:
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
    memory = await loop.run_in_executor(None, _get_memory)
    if memory is None:
        return False

    meta = similar.get("metadata") or {}
    seen = _int_or(meta.get("seen_count"), 1) + 1
    ttl = settings.memory.procedure_ttl_days
    patch = {
        "seen_count": seen,
        "strength": "strong",
        "ttl_days": int(ttl),
        "last_reinforced_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        await loop.run_in_executor(
            None,
            lambda: memory.update(
                memory_id,
                metadata=patch,
                expiration_date=_expiration_from_ttl(ttl),
            ),
        )
        logger.info("[MemoryService] procedure reinforced id=%s seen=%d (stated as %s)",
                    memory_id, seen, strength)
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
    if not settings.memory.enabled or not content or not ctx or not ctx.user_id:
        return None

    try:
        from core.memory.pipeline import milvus_breaker
    except Exception:
        milvus_breaker = None  # type: ignore[assignment]

    loop = asyncio.get_running_loop()
    # A first _get_memory() call does mem0.Memory.from_config (~700ms); run it in the executor
    memory = await loop.run_in_executor(None, _get_memory)
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
        "memory_type": MEMORY_TYPE_PROCEDURAL,
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

    try:
        result = await loop.run_in_executor(
            None,
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
        logger.debug("[MemoryService] procedure saved user=%s ws=%s id=%s",
                     ctx.user_id, ctx.workspace_id, memory_id)
        return memory_id
    except Exception as exc:
        logger.warning("[MemoryService] save_procedure_entry failed: %s", exc)
        if milvus_breaker is not None and _is_connection_error(exc):
            milvus_breaker.record_failure()
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

        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        age_days = max(0, (now - dt).days)

        # Exponential decay: 70% base score + 30% decayed score
        decay = math.exp(-0.01 * age_days)
        return base_score * (0.7 + 0.3 * decay)
    except Exception:
        return base_score


async def save_conversation(user_id: str, user_message: str, assistant_message: str) -> None:
    """
    Call mem0.Memory.add() to save conversation memory in the background.
    This function should be invoked via asyncio.create_task() and never block the main flow.
    """
    if not settings.memory.enabled or not user_id:
        return
    messages = [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": assistant_message},
    ]
    for attempt in range(2):
        memory = _get_memory()
        if memory is None:
            return
        try:
            loop = asyncio.get_running_loop()
            logger.info("[MemoryService] 开始保存记忆, user_id=%s, msg_len=%d", user_id, len(user_message))
            result = await loop.run_in_executor(
                None,
                lambda: memory.add(messages, user_id=user_id)
            )
            logger.info("[MemoryService] 用户 %s 的记忆已保存, result=%s", user_id, result)
            return
        except Exception as exc:
            if attempt == 0 and _is_connection_error(exc):
                logger.warning("[MemoryService] Milvus 连接断开，正在重置并重试: %s", exc)
                _reset_memory()
                continue
            logger.error("[MemoryService] 记忆保存失败: %s", exc, exc_info=True)


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
    if not settings.memory.enabled or not user_id:
        return []

    filters: dict = {"user_id": user_id}
    if workspace_id:
        filters["workspace_id"] = workspace_id

    for attempt in range(2):
        memory = _get_memory()
        if memory is None:
            return []
        try:
            loop = asyncio.get_running_loop()
            # mem0 2.0+: user_id must go into filters; workspace_id filters as metadata
            result = await loop.run_in_executor(
                None,
                lambda: memory.get_all(filters=filters, top_k=top_k)
            )
            if isinstance(result, dict):
                return result.get("results", [])
            if isinstance(result, list):
                return result
            return []
        except Exception as exc:
            if attempt == 0 and _is_connection_error(exc):
                logger.warning("[MemoryService] Milvus 连接断开，正在重置并重试: %s", exc)
                _reset_memory()
                continue
            logger.warning("[MemoryService] 获取记忆列表失败: %s", exc)
            return []
    return []


async def update_memory(memory_id: str, content: str) -> bool:
    """Rewrite one memory's text in place, keeping its id and metadata.

    The turn card shows what was just written and lets the user correct it. That
    correction has to be an *edit*, not delete-and-rewrite: a new id would
    detach the entry from the card that produced it and from its audit trail,
    so a user fixing a typo would silently lose the memory's history.
    """
    if not memory_id or not (content or "").strip():
        return False
    for attempt in range(2):
        memory = _get_memory()
        if memory is None:
            return False
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: memory.update(memory_id, content.strip())
            )
            return True
        except Exception as exc:
            if attempt == 0 and _is_connection_error(exc):
                logger.warning("[MemoryService] Milvus 连接断开，正在重置并重试: %s", exc)
                _reset_memory()
                continue
            logger.warning("[MemoryService] 单条更新失败: %s", exc)
            return False
    return False


async def delete_memory(memory_id: str) -> bool:
    """Delete a single memory entry."""
    for attempt in range(2):
        memory = _get_memory()
        if memory is None:
            return False
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: memory.delete(memory_id))
            return True
        except Exception as exc:
            if attempt == 0 and _is_connection_error(exc):
                logger.warning("[MemoryService] Milvus 连接断开，正在重置并重试: %s", exc)
                _reset_memory()
                continue
            logger.warning("[MemoryService] 单条删除失败: %s", exc)
            return False
    return False


async def delete_all_memories(user_id: str) -> bool:
    """Clear all memories of a user."""
    if not settings.memory.enabled or not user_id:
        return False
    for attempt in range(2):
        memory = _get_memory()
        if memory is None:
            return False
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: memory.delete_all(user_id=user_id))
            return True
        except Exception as exc:
            if attempt == 0 and _is_connection_error(exc):
                logger.warning("[MemoryService] Milvus 连接断开，正在重置并重试: %s", exc)
                _reset_memory()
                continue
            logger.warning("[MemoryService] 批量删除失败: %s", exc)
            return False
    return False
