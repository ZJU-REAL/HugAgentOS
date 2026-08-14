"""去重：判断新抽取的条目是否与既有页面同指一物。

两层结构：先用便宜的字面相似度**预筛**出每个新条目自己的候选页（避免把全库页面
塞进提示词），再让模型在这些候选里做最终判断。

判定原则是 **related ≠ same**：只合并同一事物的不同叫法（缩写、译名、全简称），
不合并"相关的两件事"。宁可留两个页面讲同一件事——那只是冗余；错误合并会把两件
不同事物的事实搅在一页，那是污染。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence, Set, Tuple

from core.db.models import KBWikiPage
from core.evolution.similarity import _ngrams, lexical_similarity
from core.kb.wiki.config import WikiConfig
from core.kb.wiki.extract import CandidateItem
from core.kb.wiki.llm import LLMBudget, call_llm_json
from core.kb.wiki.prompts import DEDUPLICATION_PROMPT
from core.kb.wiki.slug import normalize_slug

logger = logging.getLogger(__name__)

# 每个新条目最多预筛几个候选页交给模型
_MAX_CANDIDATES_PER_ITEM = 5
# 相似度门槛：低于此值不进候选，直接判为不同事物
_SIMILARITY_FLOOR = 0.34
# 条目名很短，二元组比三元组更能反映「是不是同一个叫法」
_SURFACE_NGRAM = 2
# 单次调用最多处理几个条目
_MAX_ITEMS_PER_CALL = 25


def _similarity(left: str, right: str) -> float:
    """两个表面词的相似度。完全相同直接给 1.0。

    底层复用 ``core.evolution.similarity.lexical_similarity``（字符 n-gram 的
    Jaccard），条目名很短所以取 n=2——三元组在四字名上几乎没有重叠可言。
    """
    if not left or not right:
        return 0.0
    if left.strip().lower() == right.strip().lower():
        return 1.0
    return lexical_similarity(left, right, _SURFACE_NGRAM)


def _best_pair_score_cached(
    item_surfaces: Sequence[str], page_grams: Sequence[Set[str]], page_surfaces: Sequence[str]
) -> float:
    """新条目与既有页的最佳表面词配对得分，页侧 n-gram 用预先算好的。

    页面的 n-gram 集合只依赖页面本身，却会被每一个新条目重新构造一遍——
    100 个新条目 × 2000 个页面就是十几万次重复的集合构造，纯 CPU 且在锁内。
    """
    best = 0.0
    for left in item_surfaces:
        if not left:
            continue
        stripped = left.strip().lower()
        left_grams = _ngrams(left, _SURFACE_NGRAM)
        for surface, grams in zip(page_surfaces, page_grams):
            if not surface:
                continue
            if stripped == surface.strip().lower():
                return 1.0
            if not left_grams or not grams:
                continue
            union = len(left_grams | grams)
            if union:
                best = max(best, len(left_grams & grams) / union)
    return best


def _page_surfaces(page: KBWikiPage) -> List[str]:
    return [page.title or "", *(page.aliases or [])]


def select_candidates(
    item: CandidateItem,
    pages: Sequence[KBWikiPage],
    gram_cache: Optional[Dict[str, List[Set[str]]]] = None,
) -> List[Tuple[KBWikiPage, float]]:
    """为一个新条目预筛候选既有页。

    类型必须一致：实体只与实体比，概念只与概念比。跨类型的"同名"几乎总是两件
    不同的事（比如某公司与以它命名的方法论）。

    ``gram_cache`` 由调用方跨条目复用，避免同一批里把页面的 n-gram 重算 N 遍。
    """
    item_surfaces = [item.name, *item.aliases]
    scored: List[Tuple[KBWikiPage, float]] = []
    for page in pages:
        if page.slug == item.slug or page.page_type != item.page_type:
            continue
        surfaces = _page_surfaces(page)
        if gram_cache is None:
            grams = [_ngrams(s, _SURFACE_NGRAM) for s in surfaces]
        else:
            grams = gram_cache.get(page.slug)
            if grams is None:
                grams = [_ngrams(s, _SURFACE_NGRAM) for s in surfaces]
                gram_cache[page.slug] = grams
        score = _best_pair_score_cached(item_surfaces, grams, surfaces)
        if score >= _SIMILARITY_FLOOR:
            scored.append((page, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:_MAX_CANDIDATES_PER_ITEM]


def _render_items(groups: Sequence[Tuple[CandidateItem, List[Tuple[KBWikiPage, float]]]]) -> str:
    lines = []
    for item, candidates in groups:
        alias_text = "，".join(item.aliases[:5])
        lines.append(f'  <item slug="{item.slug}" name="{item.name}" type="{item.page_type}">')
        if alias_text:
            lines.append(f"    <aliases>{alias_text}</aliases>")
        if item.description:
            lines.append(f"    <description>{item.description[:120]}</description>")
        lines.append("    <candidates>")
        for page, _score in candidates:
            page_aliases = "，".join((page.aliases or [])[:5])
            summary = (page.summary or "")[:120]
            lines.append(
                f'      <page slug="{page.slug}" name="{page.title}">'
                + (f"（别名：{page_aliases}）" if page_aliases else "")
                + summary
                + "</page>"
            )
        lines.append("    </candidates>")
        lines.append("  </item>")
    return "\n".join(lines)


def _parse_merges(payload: Optional[Dict[str, object]]) -> Dict[str, str]:
    if not payload:
        return {}
    raw = payload.get("merges")
    if not isinstance(raw, dict):
        return {}
    merges: Dict[str, str] = {}
    for source, target in raw.items():
        src = normalize_slug(source)
        dst = normalize_slug(target)
        if src and dst and src != dst:
            merges[src] = dst
    return merges


def resolve_duplicates(
    items: Sequence[CandidateItem],
    existing_pages: Sequence[KBWikiPage],
    *,
    config: WikiConfig,
    budget: LLMBudget,
) -> Dict[str, str]:
    """返回 ``{新条目 slug: 应并入的既有页 slug}``。

    只有通过了「类型一致 + 预筛相似 + 模型确认 + 目标确在自己的候选里」四道关的
    合并才会生效。任何一道不过就当作两件不同的事，各自建页。
    """
    if not items or not existing_pages:
        return {}

    groups = []
    gram_cache: Dict[str, List[Set[str]]] = {}
    for item in items:
        candidates = select_candidates(item, existing_pages, gram_cache)
        if candidates:
            groups.append((item, candidates))
    if not groups:
        return {}

    merges: Dict[str, str] = {}
    for start in range(0, len(groups), _MAX_ITEMS_PER_CALL):
        window = groups[start : start + _MAX_ITEMS_PER_CALL]
        prompt = DEDUPLICATION_PROMPT.format(candidates=_render_items(window))
        payload = call_llm_json(
            system=None,
            user=prompt,
            budget=budget,
            temperature=0.0,
            max_tokens=2000,
            purpose="去重判定",
        )
        parsed = _parse_merges(payload)
        # 硬校验：目标必须真的出现在该条目自己的候选清单里，防止模型跨条目乱指
        allowed = {item.slug: {page.slug for page, _ in candidates} for item, candidates in window}
        for source, target in parsed.items():
            if target in allowed.get(source, set()):
                merges[source] = target
            elif source in allowed:
                logger.info("Wiki 去重拒绝越界合并：%s → %s（不在其候选清单内）", source, target)

    if merges:
        logger.info("Wiki 去重合并 %d 组：%s", len(merges), merges)
    return merges


def apply_merges(items: Sequence[CandidateItem], merges: Dict[str, str]) -> List[CandidateItem]:
    """把被判定为重复的条目折叠进目标 slug。

    折叠时把源条目的别名与引文并进目标：合并的意义就在于两边的证据要汇总到
    一页上，只改 slug 不搬证据等于丢数据。
    """
    if not merges:
        return list(items)

    by_slug: Dict[str, CandidateItem] = {}
    order: List[str] = []
    for item in items:
        target_slug = merges.get(item.slug, item.slug)
        existing = by_slug.get(target_slug)
        if existing is None:
            if target_slug != item.slug:
                # 并入一个本批没有的既有页：保留原名作为别名，slug 换成目标
                item.aliases = [a for a in [item.name, *item.aliases] if a]
                item.slug = target_slug
                item.page_type = target_slug.split("/", 1)[0]
            by_slug[target_slug] = item
            order.append(target_slug)
            continue
        for alias in [item.name, *item.aliases]:
            if alias and alias not in existing.aliases and alias != existing.name:
                existing.aliases.append(alias)
        for chunk_id in item.chunk_ids:
            if chunk_id not in existing.chunk_ids:
                existing.chunk_ids.append(chunk_id)
        if not existing.description:
            existing.description = item.description
        if not existing.details:
            existing.details = item.details
    return [by_slug[slug] for slug in order]
