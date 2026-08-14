"""目录规划：为一批新条目一次性分配目录路径。

为什么整批一次而不是每页各自决定：并行写页时，每一页都会自创目录，在**建库
第一批**上发散得最厉害——那时库里还没有任何既有目录可供参照。整批一次规划能
让同类条目落到同一个目录，并强制复用已有文件夹。

规划结果只应用到**尚未归档**的页面，人工整理过或此前已归位的页面不动。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from core.db.models import KBWikiFolder
from core.kb.wiki.config import WikiConfig
from core.kb.wiki.extract import CandidateItem
from core.kb.wiki.llm import LLMBudget, call_llm_json
from core.kb.wiki.prompts import TAXONOMY_PLAN_PROMPT
from core.kb.wiki.slug import normalize_slug
from core.kb.wiki.store import clean_category_path
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# 展示给模型的既有目录上限：目录树太大时全量塞进提示词不划算
_MAX_EXISTING_FOLDERS = 120
# 单次规划的条目上限，超出分批
_MAX_ITEMS_PER_CALL = 60


def _format_existing_folders(db: Session, kb_id: str) -> str:
    rows = (
        db.query(KBWikiFolder)
        .filter(KBWikiFolder.kb_id == kb_id, KBWikiFolder.deleted_at.is_(None))
        .order_by(KBWikiFolder.depth, KBWikiFolder.name)
        .limit(_MAX_EXISTING_FOLDERS)
        .all()
    )
    if not rows:
        return "（知识库还没有任何目录，请为这批条目新建一套连贯的目录）"
    return "\n".join(f"- {row.path or row.name}" for row in rows)


def _format_items(items: Sequence[CandidateItem]) -> str:
    lines = []
    for item in items:
        desc = (item.description or item.details or "")[:100]
        lines.append(f'  <item slug="{item.slug}" name="{item.name}">{desc}</item>')
    return "\n".join(lines)


def _parse_assignments(payload: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    if not payload:
        return {}
    raw = payload.get("assignments")
    if not isinstance(raw, list):
        return {}
    result: Dict[str, List[str]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        slug = normalize_slug(entry.get("slug"))
        if not slug:
            continue
        path = entry.get("path")
        if not isinstance(path, list):
            continue
        cleaned = clean_category_path(path)
        result[slug] = cleaned
    return result


def plan_taxonomy(
    db: Session,
    kb_id: str,
    items: Sequence[CandidateItem],
    *,
    config: WikiConfig,
    budget: LLMBudget,
) -> Dict[str, List[str]]:
    """为这批条目规划目录路径。返回 ``{slug: [目录标签...]}``。

    规划失败的条目不出现在返回值里，调用方按「不指定目录」处理——页面挂在根下
    总好过挂进一个臆造的目录。
    """
    if not items:
        return {}

    existing = _format_existing_folders(db, kb_id)
    assignments: Dict[str, List[str]] = {}

    for start in range(0, len(items), _MAX_ITEMS_PER_CALL):
        window = items[start : start + _MAX_ITEMS_PER_CALL]
        prompt = TAXONOMY_PLAN_PROMPT.format(
            existing_folders=existing,
            items=_format_items(window),
            language=config.language,
        )
        payload = call_llm_json(
            system=None,
            user=prompt,
            budget=budget,
            temperature=0.0,
            max_tokens=4000,
            purpose="目录规划",
        )
        parsed = _parse_assignments(payload)
        window_slugs = {item.slug for item in window}
        # 只采纳本窗口内的 slug，模型偶发的越界输出直接丢
        assignments.update({k: v for k, v in parsed.items() if k in window_slugs})

    logger.info("Wiki 目录规划：%d/%d 个条目获得路径", len(assignments), len(items))
    return assignments
