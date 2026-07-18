"""Lightweight KB-catalog section for system-prompt injection.

Builds a minimal (name+description) KB list per the user's enabled KBs, cached.
Extracted from prompts/prompt_runtime.py; that module re-exports these.
"""

from __future__ import annotations

from threading import Lock
from time import monotonic
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Lightweight KB catalog — name + description only (no document lists).
# Injected into system prompt per user's enabled_kbs, from cached data.
# ---------------------------------------------------------------------------
_kb_lite_cache_lock = Lock()
# key: frozenset of enabled_kb_ids -> (expires_at, section_text)
_kb_lite_cache: Dict[frozenset, Tuple[float, str]] = {}
_KB_LITE_CACHE_TTL = 300.0  # 5 minutes


def invalidate_kb_lite_cache() -> None:
    """Clear lightweight KB catalog cache."""
    with _kb_lite_cache_lock:
        _kb_lite_cache.clear()


def _build_kb_lite_section(enabled_kb_ids: Optional[List[str]]) -> str:
    """Build a minimal KB catalog (name + description) for system prompt injection.

    Only uses cached Dify dataset list (no extra API calls) and fast DB queries.
    Typical output: 3-10 lines, 300-800 chars.
    """
    if not enabled_kb_ids:
        return ""

    cache_key = frozenset(enabled_kb_ids)
    now = monotonic()

    with _kb_lite_cache_lock:
        cached = _kb_lite_cache.get(cache_key)
        if cached is not None:
            expires_at, text = cached
            if now < expires_at:
                return text

    import logging
    _log = logging.getLogger(__name__)

    dify_ids = [kid for kid in enabled_kb_ids if not kid.startswith("kb_")]
    local_ids = [kid for kid in enabled_kb_ids if kid.startswith("kb_")]

    lines: List[str] = []

    # ── Public datasets (Dify) — from cached list, no extra HTTP calls ────
    if dify_ids:
        try:
            from core.kb.dify_kb import is_dify_enabled, list_datasets
            if is_dify_enabled():
                dify_set = set(dify_ids)
                datasets = list_datasets(page=1, limit=100, timeout=(1, 2))
                for ds in datasets:
                    ds_id = str(ds.get("id", "")).strip()
                    if ds_id and ds_id in dify_set:
                        name = ds.get("name", ds_id)
                        desc = ds.get("description") or ds.get("desc") or ""
                        desc_part = f"：{desc[:120]}" if desc else ""
                        lines.append(f"- {name}（公有，dataset_id: `{ds_id}`）{desc_part}")
        except Exception as exc:
            _log.debug("[kb_lite] Dify list failed: %s", exc)

    # ── Private KBs — fast DB query ───────────────────────────────────────
    if local_ids:
        try:
            from core.db.engine import SessionLocal
            from core.db.models import KBSpace
            with SessionLocal() as db:
                spaces = db.query(KBSpace).filter(
                    KBSpace.kb_id.in_(local_ids),
                    KBSpace.deleted_at.is_(None),
                ).all()
                for s in spaces:
                    desc_part = f"：{s.description[:120]}" if s.description else ""
                    lines.append(f"- {s.name}（私有，kb_id: `{s.kb_id}`）{desc_part}")
        except Exception as exc:
            _log.debug("[kb_lite] DB query failed: %s", exc)

    if not lines:
        return ""

    result = (
        "## 当前启用的知识库\n"
        "当用户提问涉及以下知识库名称或简介中的关键词时，应**主动**调用对应检索工具，无需等待用户显式要求。\n"
        "调用 `list_datasets` 可获取更详细的文档列表。\n\n"
        + "\n".join(lines)
    )

    with _kb_lite_cache_lock:
        _kb_lite_cache[cache_key] = (monotonic() + _KB_LITE_CACHE_TTL, result)

    return result
