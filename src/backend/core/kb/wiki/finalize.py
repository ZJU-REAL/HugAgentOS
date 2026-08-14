"""Finalize：一批摄入结束后的收尾。

这一阶段**不调 LLM**，全是 SQL 与图操作：

* 清死链——指向已不存在页面的 ``[[slug]]`` 降级成纯文本；
* 重建反向链接——``in_links`` 由全库 ``out_links`` 反推，而不是增量维护
  （增量维护在页面被删/改名时几乎必然漏，全量重建才是可靠的）；
* 重建索引页——一段导语 + 按类型分组的目录清单；
* 修剪空目录。

之所以单独成一个阶段而不是每写完一页就做：反向链接和死链都是**全局**性质的，
只有等这一批页面全部落库后算才准。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Set

from core.db.models.kb_wiki import (
    WIKI_KNOWLEDGE_PAGE_TYPES,
    WIKI_PAGE_TYPE_INDEX,
    WIKI_PAGE_TYPE_LABELS,
    WIKI_PAGE_TYPE_SUMMARY,
)
from core.kb.wiki.config import WikiConfig
from core.kb.wiki.llm import LLMBudget, WikiLLMError, call_llm
from core.kb.wiki.prompts import INDEX_INTRO_PROMPT, append_business_instructions
from core.kb.wiki.slug import extract_link_slugs, strip_links_except
from core.kb.wiki.store import all_pages, get_page, prune_empty_folders, upsert_page
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

INDEX_SLUG = "index/main"
# 索引页每个分组最多列多少条，避免上万页的库把索引页撑爆
_MAX_INDEX_ITEMS_PER_GROUP = 200
# 生成导语时参考多少篇文档摘要
_MAX_INTRO_SUMMARIES = 30
# 导语生成失败时的中性兜底，别让收尾因为一次模型抽风就停下
_DEFAULT_INTRO = "# 知识库索引\n\n本页汇总了知识库中已生成的全部条目。"


def finalize_kb_wiki(
    db: Session, kb_id: str, *, config: WikiConfig, budget: LLMBudget
) -> Dict[str, int]:
    """跑完整的收尾流程，返回各步骤的处理计数。"""
    pages = [p for p in all_pages(db, kb_id) if p.page_type != WIKI_PAGE_TYPE_INDEX]
    live_slugs: Set[str] = {p.slug for p in pages}

    dead_links_removed = _strip_dead_links(pages, live_slugs)
    _rebuild_backlinks(pages)
    _rebuild_index_page(db, kb_id, pages, config=config, budget=budget)
    pruned = prune_empty_folders(db, kb_id)

    db.flush()
    stats = {
        "pages": len(pages),
        "dead_links_removed": dead_links_removed,
        "folders_pruned": pruned,
    }
    logger.info("Wiki 收尾完成 kb=%s: %s", kb_id, stats)
    return stats


def _strip_dead_links(pages: List, live_slugs: Set[str]) -> int:
    """把指向不存在页面的链接降级成纯文本，并同步 out_links。"""
    removed = 0
    for page in pages:
        current = extract_link_slugs(page.content or "")
        dead = [slug for slug in current if slug not in live_slugs or slug == page.slug]
        if not dead:
            # 即便正文没问题，out_links 也可能因为此前页面被删而过期
            page.out_links = [
                s for s in (page.out_links or []) if s in live_slugs and s != page.slug
            ]
            continue
        page.content = strip_links_except(page.content or "", live_slugs, self_slug=page.slug)
        page.out_links = extract_link_slugs(page.content)
        removed += len(dead)
    return removed


def _rebuild_backlinks(pages: List) -> None:
    """由全库 out_links 反推 in_links。全量重建，不做增量。"""
    backlinks: Dict[str, List[str]] = {page.slug: [] for page in pages}
    for page in pages:
        for target in page.out_links or []:
            if target in backlinks and page.slug not in backlinks[target]:
                backlinks[target].append(page.slug)
    for page in pages:
        page.in_links = backlinks.get(page.slug, [])


def _rebuild_index_page(
    db: Session, kb_id: str, pages: List, *, config: WikiConfig, budget: LLMBudget
) -> None:
    """重建全库索引页：导语 + 按类型分组的清单。

    导语只在**首次**生成时调用模型，之后沿用——它描述的是知识库的主题领域，不会
    因为多进了几篇文档就变，每批重写既费钱又会让文案来回抖动。
    """
    existing = get_page(db, kb_id, INDEX_SLUG)
    intro = (existing.page_metadata or {}).get("intro") if existing else None
    if not intro:
        intro = _generate_intro(pages, config=config, budget=budget)

    groups = _group_pages(pages)
    body_parts = [intro.strip()] if intro else []
    for page_type, items in groups:
        label = WIKI_PAGE_TYPE_LABELS.get(page_type, page_type)
        body_parts.append(f"\n## {label}（{len(items)}）\n")
        for item in items[:_MAX_INDEX_ITEMS_PER_GROUP]:
            summary = (item.summary or "").strip()
            line = f"- [[{item.slug}|{item.title}]]"
            if summary:
                line += f" — {summary}"
            body_parts.append(line)
        if len(items) > _MAX_INDEX_ITEMS_PER_GROUP:
            body_parts.append(f"- …… 另有 {len(items) - _MAX_INDEX_ITEMS_PER_GROUP} 条")

    content = "\n".join(body_parts).strip()
    page = upsert_page(
        db,
        kb_id,
        slug=INDEX_SLUG,
        title="知识库索引",
        page_type=WIKI_PAGE_TYPE_INDEX,
        content=content,
        summary=f"全库共 {len(pages)} 个条目的总索引。",
        out_links=[p.slug for p in pages][: _MAX_INDEX_ITEMS_PER_GROUP * 3],
        category_path=[],
    )
    metadata = dict(page.page_metadata or {})
    metadata["intro"] = intro or ""
    page.page_metadata = metadata


def _group_pages(pages: List) -> List:
    """按类型分组：知识类四种合并排前，文档摘要单独一组殿后。"""
    grouped: List = []
    for page_type in (*WIKI_KNOWLEDGE_PAGE_TYPES, WIKI_PAGE_TYPE_SUMMARY):
        items = sorted(
            (p for p in pages if p.page_type == page_type), key=lambda p: (p.title or p.slug)
        )
        if items:
            grouped.append((page_type, items))
    return grouped


def _generate_intro(pages: List, *, config: WikiConfig, budget: LLMBudget) -> str:
    """用文档摘要页写一段索引导语。失败时给一句中性兜底，不阻断收尾。"""
    summaries = [
        f"- {p.title}：{p.summary}"
        for p in pages
        if p.page_type == WIKI_PAGE_TYPE_SUMMARY and (p.summary or "").strip()
    ][:_MAX_INTRO_SUMMARIES]
    if not summaries:
        return _DEFAULT_INTRO

    prompt = INDEX_INTRO_PROMPT.format(
        document_summaries="\n".join(summaries), language=config.language
    )
    prompt = append_business_instructions(prompt, config.content_instructions, "content")
    try:
        raw = call_llm(
            system=None,
            user=prompt,
            budget=budget,
            temperature=0.3,
            max_tokens=800,
            purpose="索引导语",
        )
    except WikiLLMError as exc:
        logger.warning("Wiki 索引导语生成失败，使用兜底文案: %s", exc)
        return _DEFAULT_INTRO
    return (raw or "").strip() or _DEFAULT_INTRO
