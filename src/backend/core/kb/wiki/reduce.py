"""Reduce：按 slug 写页。

这一阶段模型的角色是**编译器，不是写手**——它拿到的是逐字的原文分块，任务是把
它们编排进页面，而不是重写成漂亮散文。提示词里那些"不许润色""不许加旨在/致力于"
的硬约束就是干这个的，改提示词时不要削弱。

三道落库前的清洗一个都不能少：
1. 还原 slug 句柄（模型看到的是 ref-N，库里存真 slug）；
2. 剔除白名单外的链接与自链接（模型会臆造 slug）；
3. 自动织链补上模型漏掉的引用。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from core.kb.wiki.config import WikiConfig
from core.kb.wiki.extract import CandidateItem
from core.kb.wiki.linkify import linkify_content
from core.kb.wiki.llm import LLMBudget, WikiLLMError, call_llm, split_summary_line
from core.kb.wiki.prompts import (
    DOCUMENT_SUMMARY_PROMPT,
    PAGE_MODIFY_SYSTEM_PROMPT,
    PAGE_MODIFY_USER_PROMPT,
    append_business_instructions,
)
from core.kb.wiki.slug import SlugHandles, extract_link_slugs, strip_links_except

logger = logging.getLogger(__name__)

# 单页送进模型的证据总量上限
_MAX_EVIDENCE_CHARS = 24000
# 有效链接清单里最多列多少个 slug
_MAX_LINK_CANDIDATES = 120
_PAGE_TYPE_LABELS = {"entity": "实体", "concept": "概念", "summary": "文档摘要"}


def _render_evidence(chunks: Sequence[Any], handles_note: str = "") -> str:
    """把支撑分块渲染成证据块。逐字给出，不做任何加工。"""
    parts: List[str] = []
    total = 0
    for index, chunk in enumerate(chunks, start=1):
        content = str(getattr(chunk, "content", "") or "").strip()
        if not content:
            continue
        remaining = _MAX_EVIDENCE_CHARS - total
        if remaining <= 0:
            break
        snippet = content[:remaining]
        total += len(snippet)
        parts.append(f"【素材 {index}】\n{snippet}")
    body = "\n\n".join(parts)
    return f"{handles_note}{body}" if handles_note else body


def _render_link_candidates(targets: Dict[str, str], handles: SlugHandles) -> str:
    """有效链接清单：``[[句柄]] = 展示名``。"""
    lines = []
    for slug, title in list(targets.items())[:_MAX_LINK_CANDIDATES]:
        lines.append(f"- [[{handles.handle_for(slug)}]] = {title}")
    return "\n".join(lines) if lines else "（暂无可用的关联页面）"


def _clean_output(
    raw: str,
    *,
    handles: SlugHandles,
    valid_slugs: Sequence[str],
    self_slug: str,
    link_targets: Dict[str, Sequence[str]],
) -> tuple[str, str, List[str]]:
    """还原句柄 → 清死链 → 自动织链，返回 ``(摘要, 正文, 出链)``。"""
    summary, body = split_summary_line(raw)
    body = handles.restore(body)
    summary = handles.restore(summary)
    body = strip_links_except(body, valid_slugs, self_slug=self_slug)
    body = linkify_content(body, link_targets, self_slug=self_slug)
    return summary.strip(), body.strip(), extract_link_slugs(body)


def write_entity_page(
    *,
    item: CandidateItem,
    chunks: Sequence[Any],
    existing_content: str,
    link_targets: Dict[str, Sequence[str]],
    source_contexts: str,
    config: WikiConfig,
    budget: LLMBudget,
) -> Optional[Dict[str, Any]]:
    """写一个实体页/概念页。返回 None 表示本页这轮跳过（保留既有内容）。"""
    if not chunks:
        return None

    titles = {slug: (surfaces[0] if surfaces else slug) for slug, surfaces in link_targets.items()}
    handles = SlugHandles([item.slug, *titles.keys()])

    context_block = ""
    if source_contexts:
        context_block = (
            f"<shared_source_contexts>\n{source_contexts}\n</shared_source_contexts>\n\n"
        )

    new_info_block = (
        "\n<new_information>\n"
        + _render_evidence(chunks)
        + "\n</new_information>\n\n"
        + "上面的 <new_information> 由已确认直接支撑本页的来源分块**逐字**拼装而成。"
        + "前面的 <shared_source_contexts> 只是背景框架，不是证据。\n"
    )

    user_prompt = PAGE_MODIFY_USER_PROMPT.format(
        shared_source_contexts_block=context_block,
        page_slug=item.slug,
        page_title=item.name,
        page_type=item.page_type,
        page_type_label=_PAGE_TYPE_LABELS.get(item.page_type, "条目"),
        page_aliases="，".join(item.aliases[:8]),
        existing_content=existing_content or "（这是一个新页面，尚无既有内容）",
        new_information_block=new_info_block,
        deleted_block="",
        available_slugs=_render_link_candidates(titles, handles),
        language=config.language,
    )

    try:
        raw = call_llm(
            system=append_business_instructions(
                PAGE_MODIFY_SYSTEM_PROMPT, config.content_instructions, "content"
            ),
            user=user_prompt,
            budget=budget,
            temperature=0.2,
            max_tokens=6000,
            purpose="写页",
        )
    except WikiLLMError as exc:
        logger.warning("Wiki 写页失败 %s: %s", item.slug, exc)
        return None

    summary, body, out_links = _clean_output(
        raw,
        handles=handles,
        valid_slugs=list(titles.keys()),
        self_slug=item.slug,
        link_targets=link_targets,
    )
    if not body:
        return None

    return {
        "slug": item.slug,
        "title": item.name,
        "page_type": item.page_type,
        "content": body,
        "summary": summary or item.description,
        "aliases": item.aliases,
        "out_links": out_links,
    }


def write_document_summary(
    *,
    slug: str,
    title: str,
    content: str,
    link_targets: Dict[str, Sequence[str]],
    config: WikiConfig,
    budget: LLMBudget,
) -> Optional[Dict[str, Any]]:
    """为一篇源文档写摘要页。

    有意不把文件名喂给模型：上传的文档常带毫无信息量的文件名（按扫描仪型号命名
    之类），正文一旦提取得稀薄，文件名就会诱发臆造。
    """
    text = (content or "").strip()
    if not text:
        return None

    titles = {s: (surfaces[0] if surfaces else s) for s, surfaces in link_targets.items()}
    handles = SlugHandles(list(titles.keys()))

    prompt = DOCUMENT_SUMMARY_PROMPT.format(
        content=text[:_MAX_EVIDENCE_CHARS],
        available_slugs=_render_link_candidates(titles, handles),
        language=config.language,
    )
    prompt = append_business_instructions(prompt, config.content_instructions, "content")
    try:
        raw = call_llm(
            system=None,
            user=prompt,
            budget=budget,
            temperature=0.2,
            max_tokens=4000,
            purpose="文档摘要",
        )
    except WikiLLMError as exc:
        logger.warning("Wiki 文档摘要失败 %s: %s", slug, exc)
        return None

    summary, body, out_links = _clean_output(
        raw,
        handles=handles,
        valid_slugs=list(titles.keys()),
        self_slug=slug,
        link_targets=link_targets,
    )
    if not body:
        return None

    return {
        "slug": slug,
        "title": title,
        "page_type": "summary",
        "content": body,
        "summary": summary,
        "aliases": [],
        "out_links": out_links,
    }
