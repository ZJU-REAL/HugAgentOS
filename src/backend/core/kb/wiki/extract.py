"""Pass 0：候选条目抽取。

从整篇文档里抽出实体/概念的**骨架**——名称、slug、别名、一句话说明、一段兜底
简述。这一轮刻意不要详尽事实：哪几个分块支撑了它，交给下一轮引文标注去挂，
这样即便文档很长，本轮也保持便宜。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from core.kb.wiki.config import WikiConfig
from core.kb.wiki.llm import LLMBudget, call_llm_json
from core.kb.wiki.prompts import (
    CANDIDATE_SLUG_PROMPT,
    append_business_instructions,
    granularity_guidance,
)
from core.kb.wiki.slug import normalize_slug

logger = logging.getLogger(__name__)

# 送进抽取的正文上限。超长文档截断——候选抽取要的是"这篇讲了哪些事物"，
# 前若干万字足以覆盖；漏掉的条目还有引文阶段的 new_slugs 兜底。
_MAX_EXTRACT_CHARS = 40000
_MAX_ITEMS = 200


@dataclass
class CandidateItem:
    """一个候选实体/概念。"""

    slug: str
    name: str
    page_type: str  # entity | concept
    description: str = ""
    details: str = ""
    aliases: List[str] = field(default_factory=list)
    # 引文阶段填：支撑本条目的父块 ID
    chunk_ids: List[str] = field(default_factory=list)

    def surfaces(self) -> List[str]:
        """可用于正文织链的表面词：标题 + 别名。"""
        return [self.name, *self.aliases]


def _coerce_items(raw: Any, page_type: str) -> List[CandidateItem]:
    items: List[CandidateItem] = []
    if not isinstance(raw, list):
        return items
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        slug = normalize_slug(entry.get("slug"), default_type=page_type)
        name = str(entry.get("name") or "").strip()
        if not slug or not name:
            continue
        aliases = [
            str(a).strip()
            for a in (entry.get("aliases") or [])
            if str(a or "").strip() and str(a).strip() != name
        ]
        items.append(
            CandidateItem(
                slug=slug,
                name=name,
                page_type=slug.split("/", 1)[0],
                description=str(entry.get("description") or "").strip(),
                details=str(entry.get("details") or "").strip(),
                aliases=aliases[:10],
            )
        )
    return items


def parse_candidates(payload: Optional[Dict[str, Any]]) -> List[CandidateItem]:
    """把抽取响应解析成候选列表，跨两个数组按 slug 去重。"""
    if not payload:
        return []
    items = _coerce_items(payload.get("entities"), "entity")
    items.extend(_coerce_items(payload.get("concepts"), "concept"))
    deduped: Dict[str, CandidateItem] = {}
    for item in items:
        if item.slug not in deduped:
            deduped[item.slug] = item
    return list(deduped.values())[:_MAX_ITEMS]


def render_previous_slugs(previous: Sequence[CandidateItem]) -> str:
    """把上一轮的 slug 渲染给模型看，用于强制 slug 复用。"""
    if not previous:
        return "（这是该文档首次处理，没有历史条目）"
    lines = [f"- {item.slug} = {item.name}" for item in previous]
    return "\n".join(lines)


def extract_candidates(
    *,
    content: str,
    config: WikiConfig,
    budget: LLMBudget,
    previous: Sequence[CandidateItem] = (),
) -> List[CandidateItem]:
    """跑一次候选抽取。模型失灵时返回空列表，让调用方跳过这篇文档而不是整批失败。"""
    text = (content or "").strip()
    if not text:
        return []

    prompt = CANDIDATE_SLUG_PROMPT.format(
        content=text[:_MAX_EXTRACT_CHARS],
        previous_slugs=render_previous_slugs(previous),
        language=config.language,
        granularity=config.granularity,
        granularity_guidance=granularity_guidance(config.granularity),
    )
    prompt = append_business_instructions(prompt, config.extraction_instructions, "extraction")
    payload = call_llm_json(
        system=None,
        user=prompt,
        budget=budget,
        temperature=0.1,
        max_tokens=6000,
        purpose="候选抽取",
    )
    items = parse_candidates(payload)
    logger.info("Wiki 候选抽取得到 %d 个条目（粒度 %s）", len(items), config.granularity)
    return items
