"""Local knowledge-base retrieval shared by all editions."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from core.auth.kb_permissions import get_accessible_local_kb_ids, is_shared_visibility
from core.config.runtime_env import get_runtime_value


def _read_int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        return value if value > 0 else default
    except ValueError:
        return default


LOCAL_RETRIEVE_TOTAL_TIMEOUT_SECONDS = _read_int_env("RETRIEVE_LOCAL_KB_TIMEOUT_SECONDS", 30)
LOCAL_RETRIEVE_STAGE_TIMEOUT_SECONDS = _read_int_env("RETRIEVE_LOCAL_KB_STAGE_TIMEOUT_SECONDS", 10)


class LocalKnowledgeBaseTimeoutError(TimeoutError):
    """Raised when local knowledge-base work exhausts its internal deadline."""


_local_kb_logger = logging.getLogger(__name__ + ".local_kb")


def _get_kb_detail_max_chars() -> int:
    """Admin-panel managed via knowledge_base.detail_max_chars; resolved DB→env per call."""
    raw = (get_runtime_value("KB_DETAIL_CONTENT_MAX_CHARS") or "50000").strip()
    try:
        return int(raw)
    except ValueError:
        return 50000


def _get_allowed_kb_ids() -> set[str]:
    raw = os.getenv("LOCAL_KB_ALLOWED_IDS", "").strip()
    if not raw:
        return set()
    return {k.strip() for k in raw.split(",") if k.strip()}


def _get_current_user_id() -> str:
    return os.getenv("CURRENT_USER_ID", "").strip()


def _fetch_parent_contents(parent_ids: list[str]) -> dict[str, str]:
    """Fetch parent chunk content from PostgreSQL by chunk_id list."""
    if not parent_ids:
        return {}
    try:
        from core.db.engine import SessionLocal
        from core.db.models import KBChunk

        db = SessionLocal()
        try:
            chunks = db.query(KBChunk).filter(KBChunk.chunk_id.in_(parent_ids)).all()
            return {c.chunk_id: c.content for c in chunks}
        finally:
            db.close()
    except Exception as exc:
        _local_kb_logger.warning("Failed to fetch parent chunks from DB: %s", exc)
        return {}


def _build_runtime_local_kb_section() -> str:
    """Build runtime private KB list for tool description injection.

    NOTE: 详细的知识库简介和文档列表在系统提示词中动态注入（见 prompt_runtime.py），
    此处仅提供 kb_id 与名称的快速参考。
    """
    allowed_raw = os.getenv("LOCAL_KB_ALLOWED_IDS", "").strip()
    if not allowed_raw:
        return ""

    allowed_ids = [k.strip() for k in allowed_raw.split(",") if k.strip()]
    if not allowed_ids:
        return ""

    # Try to fetch KB names from DB
    kb_names: dict[str, str] = {}
    try:
        from core.db.engine import SessionLocal
        from core.db.models import KBSpace

        db = SessionLocal()
        try:
            spaces = db.query(KBSpace).filter(KBSpace.kb_id.in_(allowed_ids)).all()
            kb_names = {s.kb_id: s.name for s in spaces}
        finally:
            db.close()
    except Exception as exc:
        _local_kb_logger.debug("Could not fetch KB names for tool description: %s", exc)

    lines = []
    for kid in allowed_ids:
        name = kb_names.get(kid, kid)
        lines.append(f"- {kid} | {name}")

    return "\n".join(
        [
            "## 当前可用本地知识库（运行时注入，含公有库与私有库）",
            "调用 `retrieve_local_kb` 时，`kb_id` 应从以下列表中选择（详细简介、可见性与文档列表见系统提示词）。",
            "注意：本列表混含公有库与私有库，不要据此把其中的库一律当作私有库。",
            "格式：`kb_id | 知识库名称`",
            *lines,
            "## 当前可用本地知识库（运行时注入）结束",
        ]
    ).strip()


def retrieve_local_kb(
    kb_id: str,
    query: str,
    top_k: int = 10,
    *,
    allowed_kb_ids: str | None = None,
    current_user_id: str | None = None,
    reranker_enabled: str | None = None,
) -> Any:  # 错误分支返回 list[dict]，成功/空命中分支返回 dict —— 异构返回，标注为 Any
    """Search user's private KB and return ranked result chunks.

    Returns a list of dicts with keys: id, title, content, kb_id, score.
    """
    deadline = time.monotonic() + LOCAL_RETRIEVE_TOTAL_TIMEOUT_SECONDS

    def _remaining_stage_timeout() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LocalKnowledgeBaseTimeoutError(
                f"私有知识库检索超过 {LOCAL_RETRIEVE_TOTAL_TIMEOUT_SECONDS}s"
            )
        return min(float(LOCAL_RETRIEVE_STAGE_TIMEOUT_SECONDS), remaining)

    # ── Auth check ──────────────────────────────────────────────────────────
    if allowed_kb_ids is not None:
        allowed = {k.strip() for k in allowed_kb_ids.split(",") if k.strip()}
    else:
        allowed = _get_allowed_kb_ids()

    user_id = current_user_id if current_user_id is not None else _get_current_user_id()

    # Auto-resolve（权限分配单一真源）：未给 allowed 列表时，按当前用户的可见集解析——
    # 自己私有库 + 公有库 + 已授权的 scoped 库。无 user_id 时降级为全部库（仅 stdio/本地调试，
    # HTTP 模式必有 X-Current-User-Id）。
    if not allowed:
        try:
            from core.db.engine import SessionLocal
            from core.db.models import KBSpace

            with SessionLocal() as _db:
                if user_id:
                    allowed = get_accessible_local_kb_ids(_db, user_id)
                else:
                    spaces = (
                        _db.query(KBSpace.kb_id)
                        .filter(
                            KBSpace.deleted_at.is_(None),
                        )
                        .all()
                    )
                    allowed = {s.kb_id for s in spaces if s.kb_id}
                _local_kb_logger.info("Auto-resolved %d KB spaces", len(allowed))
        except Exception as exc:
            _local_kb_logger.warning("Auto-resolve KB spaces failed: %s", exc)

    if not allowed:
        _local_kb_logger.warning("retrieve_local_kb: no accessible private KBs")
        return [{"error": "未找到可访问的私有知识库"}]

    kb_id = (kb_id or "").strip()
    # Determine which KBs to search
    if kb_id:
        if kb_id not in allowed:
            _local_kb_logger.warning("retrieve_local_kb: kb_id %s not in allowed list", kb_id)
            return [{"error": f"无权访问知识库 {kb_id}"}]
        search_kb_ids = [kb_id]
    else:
        # Search ALL allowed KBs
        search_kb_ids = sorted(allowed)
        _local_kb_logger.info("Searching all %d allowed KBs: %s", len(search_kb_ids), search_kb_ids)

    # Classify search targets into private (owner-isolated) vs public (global) KBs and,
    # when headers didn't supply a user_id, resolve the owner of a private space — all in
    # one query. Public KBs are admin-managed (visibility=="public") and searched by kb_id.
    public_ids: list[str] = []
    private_ids: list[str] = list(search_kb_ids)
    try:
        from core.db.engine import SessionLocal
        from core.db.models import KBSpace

        with SessionLocal() as _db:
            rows = (
                _db.query(KBSpace.kb_id, KBSpace.visibility, KBSpace.user_id)
                .filter(
                    KBSpace.kb_id.in_(search_kb_ids),
                    KBSpace.deleted_at.is_(None),
                )
                .all()
            )
            # public 与 scoped 都是「共享库」：按 kb_id 全局检索（向量行归属系统属主，
            # 不能再叠加 user_id 过滤，否则被授权用户搜不到）。仅 private 才 owner 隔离。
            shared_set = {r.kb_id for r in rows if is_shared_visibility(r.visibility)}
            public_ids = [k for k in search_kb_ids if k in shared_set]
            private_ids = [k for k in search_kb_ids if k not in shared_set]
            if private_ids and not user_id:
                user_id = next((r.user_id for r in rows if r.kb_id in private_ids), user_id)
    except Exception as exc:
        _local_kb_logger.warning("retrieve_local_kb: visibility classification failed: %s", exc)

    if private_ids and not user_id:
        return [{"error": "未能获取当前用户 ID"}]

    # ── Embed query ──────────────────────────────────────────────────────────
    try:
        from core.kb.kb_vector import embed_text, hybrid_search

        query_vec = embed_text(query, timeout=_remaining_stage_timeout())
    except LocalKnowledgeBaseTimeoutError:
        raise
    except Exception as exc:
        _local_kb_logger.error("retrieve_local_kb: embed_text failed: %s", exc)
        return [{"error": f"向量化失败：{exc}"}]

    # ── Hybrid search ────────────────────────────────────────────────────────
    try:
        hits = hybrid_search(
            user_id=user_id or "",
            kb_ids=private_ids,
            query=query,
            query_vec=query_vec,
            top_k=top_k * 3,  # over-fetch before dedup
            public_kb_ids=public_ids,
            timeout=_remaining_stage_timeout(),
        )
    except LocalKnowledgeBaseTimeoutError:
        raise
    except Exception as exc:
        _local_kb_logger.error("retrieve_local_kb: hybrid_search failed: %s", exc)
        return [{"error": f"检索失败：{exc}"}]

    # Build KB metadata for the response
    kb_meta: list[dict[str, str]] = []
    try:
        from core.db.engine import SessionLocal
        from core.db.models import KBSpace

        with SessionLocal() as _db:
            spaces = (
                _db.query(KBSpace)
                .filter(
                    KBSpace.kb_id.in_(search_kb_ids),
                )
                .all()
            )
            kb_meta = [
                {"kb_id": s.kb_id, "name": s.name, "description": s.description or ""}
                for s in spaces
            ]
    except Exception:
        pass

    if not hits:
        return {"available_kbs": kb_meta, "items": [], "message": "未找到相关内容"}

    # ── Dedup by parent_chunk_id (keep highest score) ────────────────────────
    seen: dict[str, dict] = {}
    for hit in hits:
        pid = hit.get("parent_chunk_id") or hit.get("chunk_id", "")
        if pid not in seen or hit["score"] > seen[pid]["score"]:
            seen[pid] = hit

    # Sort by score descending, take top_k
    top_hits = sorted(seen.values(), key=lambda x: x["score"], reverse=True)[:top_k]

    # ── Optional reranker step ───────────────────────────────────────────────
    _reranker_flag = (
        reranker_enabled if reranker_enabled is not None else os.getenv("RERANKER_ENABLED", "")
    )
    if (_reranker_flag or "").lower() in ("true", "1"):
        try:
            from core.kb.kb_vector import is_reranker_configured, rerank

            if is_reranker_configured() and top_hits:
                contents = [hit.get("content", "") for hit in top_hits]
                reranked = rerank(
                    query,
                    contents,
                    top_n=top_k,
                    timeout=_remaining_stage_timeout(),
                )
                reranked_hits = []
                for item in reranked:
                    idx = item.get("index", 0)
                    if 0 <= idx < len(top_hits):
                        hit = dict(top_hits[idx])
                        hit["score"] = round(item.get("relevance_score", hit["score"]), 4)
                        reranked_hits.append(hit)
                if reranked_hits:
                    top_hits = reranked_hits
                    _local_kb_logger.info("Reranker applied: %d results reranked", len(top_hits))
        except LocalKnowledgeBaseTimeoutError:
            raise
        except Exception as rerank_exc:
            _local_kb_logger.warning(
                "Reranker failed, falling back to original ranking: %s", rerank_exc
            )

    # ── Fetch parent content from PostgreSQL ─────────────────────────────────
    _remaining_stage_timeout()
    parent_ids = [h["parent_chunk_id"] for h in top_hits if h.get("parent_chunk_id")]
    parent_map = _fetch_parent_contents(parent_ids)

    # ── Build results ────────────────────────────────────────────────────────
    results = []
    total_chars = 0
    kb_detail_max_chars = _get_kb_detail_max_chars()
    for i, hit in enumerate(top_hits):
        pid = hit.get("parent_chunk_id") or hit.get("chunk_id", "")
        # Prefer parent content (full context); fall back to child snippet
        content = parent_map.get(pid) or hit.get("content", "")

        if total_chars + len(content) > kb_detail_max_chars:
            content = content[: max(0, kb_detail_max_chars - total_chars)]
            if content:
                results.append(
                    {
                        "id": pid,
                        "title": hit.get("title", ""),
                        "content": content,
                        "kb_id": hit.get("kb_id", kb_id),
                        "score": round(hit["score"], 4),
                        "chunk_index": hit.get("chunk_index", i),
                    }
                )
            break

        total_chars += len(content)
        results.append(
            {
                "id": pid,
                "title": hit.get("title", ""),
                "content": content,
                "kb_id": hit.get("kb_id", kb_id),
                "score": round(hit["score"], 4),
                "chunk_index": hit.get("chunk_index", i),
            }
        )

    return {"available_kbs": kb_meta, "items": results}
