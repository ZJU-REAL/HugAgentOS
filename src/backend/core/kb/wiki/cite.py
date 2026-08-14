"""Pass 1..N：分块引文标注。

这一轮问模型的问题非常窄：**这批分块里，哪几块实质性讨论了这个候选？** 它不产
事实、不做转述——事实以分块原文的逐字形态流向写页阶段。整条管线之所以不容易
跑偏，根子就在这里。

分块用 ``c001`` 这样的短序号 ID 出现在提示词里，而不是真实的 64 位 chunk_id：
高熵字符串会被模型抄错，而抄错一个 ID 就是一条断掉的血缘。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.kb.wiki.config import WikiConfig
from core.kb.wiki.extract import CandidateItem, parse_candidates
from core.kb.wiki.llm import LLMBudget, call_llm_json
from core.kb.wiki.prompts import CHUNK_CITATION_PROMPT, append_business_instructions
from core.kb.wiki.slug import normalize_slug

logger = logging.getLogger(__name__)

# 单块进提示词的正文上限：超长块截断，避免一块把整批预算吃光
_MAX_CHUNK_CHARS = 3000
# 候选清单里每条附带的说明长度
_MAX_DESC_CHARS = 120


def _xml_escape(text: str) -> str:
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def split_into_batches(chunks: Sequence[Any], batch_size: int) -> List[List[Tuple[str, Any]]]:
    """把分块切成批，并给每块配一个批内短 ID。

    短 ID 全局连续（c001、c002…）而非每批重置，这样把多批结果合并回来时不会撞号。
    """
    batches: List[List[Tuple[str, Any]]] = []
    current: List[Tuple[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        current.append((f"c{index:03d}", chunk))
        if len(current) >= max(1, batch_size):
            batches.append(current)
            current = []
    if current:
        batches.append(current)
    return batches


def render_candidates_xml(candidates: Sequence[CandidateItem]) -> str:
    lines = []
    for item in candidates:
        alias_text = "，".join(item.aliases[:5])
        desc = (item.description or item.details or "")[:_MAX_DESC_CHARS]
        lines.append(
            f'  <candidate slug="{_xml_escape(item.slug)}" name="{_xml_escape(item.name)}">'
            f"{_xml_escape(desc)}"
            + (f"（别名：{_xml_escape(alias_text)}）" if alias_text else "")
            + "</candidate>"
        )
    return "\n".join(lines) if lines else "  （本文档暂无候选条目）"


def render_chunks_xml(batch: Sequence[Tuple[str, Any]]) -> str:
    lines = []
    for short_id, chunk in batch:
        content = str(getattr(chunk, "content", "") or "")[:_MAX_CHUNK_CHARS]
        lines.append(f'  <c id="{short_id}">{_xml_escape(content)}</c>')
    return "\n".join(lines)


def _parse_citations(payload: Optional[Dict[str, Any]]) -> Dict[str, List[str]]:
    if not payload:
        return {}
    raw = payload.get("citations")
    if not isinstance(raw, dict):
        return {}
    result: Dict[str, List[str]] = {}
    for slug, chunk_ids in raw.items():
        normalized = normalize_slug(slug)
        if not normalized or not isinstance(chunk_ids, list):
            continue
        ids = [str(c).strip() for c in chunk_ids if str(c or "").strip()]
        if ids:
            result.setdefault(normalized, []).extend(ids)
    return result


def _parse_new_slugs(
    payload: Optional[Dict[str, Any]],
) -> Tuple[List[CandidateItem], Dict[str, List[str]]]:
    """解析 new_slugs，同时把它们自带的 source_chunks 拆成引文。"""
    if not payload:
        return [], {}
    raw = payload.get("new_slugs")
    if not isinstance(raw, list):
        return [], {}

    entities = [e for e in raw if isinstance(e, dict) and str(e.get("type") or "") == "entity"]
    concepts = [e for e in raw if isinstance(e, dict) and str(e.get("type") or "") != "entity"]
    items = parse_candidates({"entities": entities, "concepts": concepts})

    citations: Dict[str, List[str]] = {}
    by_name = {str(e.get("slug") or ""): e for e in raw if isinstance(e, dict)}
    for item in items:
        source = by_name.get(item.slug) or {}
        chunk_ids = [
            str(c).strip() for c in (source.get("source_chunks") or []) if str(c or "").strip()
        ]
        if chunk_ids:
            citations[item.slug] = chunk_ids
    return items, citations


def annotate_citations(
    *,
    candidates: Sequence[CandidateItem],
    chunks: Sequence[Any],
    config: WikiConfig,
    budget: LLMBudget,
) -> Tuple[Dict[str, List[str]], List[CandidateItem]]:
    """为候选条目标注支撑分块。

    返回 ``({slug: [真实 chunk_id]}, 本轮新发现的候选)``。某一批解析失败只影响
    那一批，其余批次照常合并——宁可少几条引文，不要整篇文档白跑。
    """
    if not candidates or not chunks:
        return {}, []

    batches = split_into_batches(chunks, config.citation_chunk_batch)
    candidates_xml = render_candidates_xml(candidates)

    merged: Dict[str, List[str]] = {}
    discovered: Dict[str, CandidateItem] = {}

    for batch in batches:
        short_to_real = {short_id: getattr(chunk, "chunk_id", "") for short_id, chunk in batch}
        prompt = CHUNK_CITATION_PROMPT.format(
            language=config.language,
            candidate_slugs=candidates_xml,
            chunks_xml=render_chunks_xml(batch),
        )
        prompt = append_business_instructions(prompt, config.extraction_instructions, "extraction")
        payload = call_llm_json(
            system=None,
            user=prompt,
            budget=budget,
            temperature=0.0,
            max_tokens=4000,
            purpose="引文标注",
        )
        if payload is None:
            continue

        new_items, new_citations = _parse_new_slugs(payload)
        for item in new_items:
            discovered.setdefault(item.slug, item)

        for slug, short_ids in {**_parse_citations(payload), **new_citations}.items():
            for short_id in short_ids:
                real_id = short_to_real.get(short_id)
                if not real_id:
                    continue  # 模型引了不在本批的 ID，丢弃
                bucket = merged.setdefault(slug, [])
                if real_id not in bucket:
                    bucket.append(real_id)

    logger.info(
        "Wiki 引文标注完成：%d 批，%d 个条目获得引文，新发现 %d 个条目",
        len(batches),
        len(merged),
        len(discovered),
    )
    return merged, list(discovered.values())


def apply_citations(
    candidates: Sequence[CandidateItem], citations: Dict[str, List[str]], *, max_chunks: int
) -> List[CandidateItem]:
    """把引文结果回填到候选条目上，并丢掉一条引文都没拿到的条目。

    没有任何分块支撑的条目多半是模型在候选阶段的过度联想——留着只会产出一页
    只有 details 兜底文字、没有出处的空壳页。
    """
    kept: List[CandidateItem] = []
    for item in candidates:
        chunk_ids = citations.get(item.slug) or []
        if not chunk_ids:
            continue
        item.chunk_ids = chunk_ids[:max_chunks]
        kept.append(item)
    return kept
