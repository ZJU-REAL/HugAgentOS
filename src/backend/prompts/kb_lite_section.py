"""Lightweight KB-catalog section for system-prompt injection.

Lists the run's enabled knowledge bases (name + description) so the model knows
when to search them. External collection names come from the shared in-memory
directory (``features.knowledge.edition.collection_snapshot``) and never wait on
the provider; private KB names come from one indexed DB query. Callers run this
off the event loop (the agent factory overlaps it with MCP connection).
"""

from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

_HEADER = (
    "## 当前启用的知识库\n"
    "当用户提问涉及以下知识库名称或简介中的关键词时，应**主动**调用对应检索工具，无需等待用户显式要求。\n"
    "调用 `list_datasets` 可获取更详细的文档列表。\n\n"
)


def _external_lines(external_ids: List[str]) -> List[str]:
    from features.knowledge.edition import collection_snapshot, is_enabled

    if not is_enabled():
        return []
    datasets = collection_snapshot()
    if datasets is None:
        # Nothing cached yet (cold process; a refresh is now running). Name the
        # collections by id so the model still knows they exist; the next turn
        # renders the full names from the refreshed directory.
        return [f"- 外部知识库（公有，dataset_id: `{ds_id}`）" for ds_id in external_ids]
    by_id = {str(ds.get("id", "")).strip(): ds for ds in datasets}
    lines: List[str] = []
    for ds_id in external_ids:
        ds = by_id.get(ds_id)
        if ds is None:
            continue
        name = ds.get("name", ds_id)
        desc = ds.get("description") or ds.get("desc") or ""
        desc_part = f"：{desc[:120]}" if desc else ""
        lines.append(f"- {name}（公有，dataset_id: `{ds_id}`）{desc_part}")
    return lines


def _local_lines(local_ids: List[str]) -> List[str]:
    from core.db.engine import SessionLocal
    from features.knowledge.models import KBSpace

    with SessionLocal() as db:
        spaces = (
            db.query(KBSpace)
            .filter(KBSpace.kb_id.in_(local_ids), KBSpace.deleted_at.is_(None))
            .all()
        )
        by_id = {space.kb_id: space for space in spaces}
    lines: List[str] = []
    for kb_id in local_ids:
        space = by_id.get(kb_id)
        if space is None:
            continue
        desc_part = f"：{space.description[:120]}" if space.description else ""
        lines.append(f"- {space.name}（私有，kb_id: `{space.kb_id}`）{desc_part}")
    return lines


def build_kb_catalog_section(enabled_kb_ids: Optional[List[str]]) -> str:
    """Render the section for ``enabled_kb_ids``; empty when none resolve. Blocking (DB)."""
    # Sorted: the section is part of the system prompt, and its bytes must not
    # depend on the order a client happened to send the ids in (LLM prefix cache).
    ids = sorted({str(kid).strip() for kid in enabled_kb_ids or [] if str(kid).strip()})
    if not ids:
        return ""
    lines: List[str] = []
    external_ids = [kid for kid in ids if not kid.startswith("kb_")]
    local_ids = [kid for kid in ids if kid.startswith("kb_")]
    if external_ids:
        try:
            lines += _external_lines(external_ids)
        except Exception as exc:  # noqa: BLE001 - the prompt is still usable without it
            logger.warning("[kb_catalog] external collection names unavailable: %s", exc)
    if local_ids:
        try:
            lines += _local_lines(local_ids)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[kb_catalog] private KB lookup failed: %s", exc)
    return _HEADER + "\n".join(lines) if lines else ""
