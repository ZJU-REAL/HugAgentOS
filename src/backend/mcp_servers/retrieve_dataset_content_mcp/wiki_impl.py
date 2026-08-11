"""LLM Wiki / 概念图谱工具的实现层（仅 Wiki-capable 后端可用）。

这组工具服务于一条**单向链**：

    ① 在地图上定位   wiki_locate   —— 用问题里的词命中概念页 / 实体页
    ② 沿关系展开     wiki_expand   —— 顺着页面的双链把相关概念一次拉齐
    ③ 顺血缘取原文   wiki_fetch_source —— 按 chunk_refs 直取原文，不再检索一次

Wiki 页面是知识库的**地图**，不是第二个检索源，也不进向量库。地图负责把范围
收窄到正确的位置，真正的答案与出处始终来自原文分块——所以 ③ 是按 ID 直取而
不是拿 Wiki 摘要当答案。

后端不支持 Wiki（dify / fastgpt / 社区版）时，这些函数不会被注册成工具，
server 侧的 list_tools 会把它们整体过滤掉。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)

# 定位阶段每页摘要的截断长度：够模型判断相关性，又不至于把上下文吃满
_SUMMARY_MAX_CHARS = 180
# 单次取回原文分块的硬上限
_MAX_SOURCE_CHUNKS = 20


class WikiUnsupportedError(RuntimeError):
    """当前知识库后端不提供 Wiki / 图谱能力。"""


def _wiki_module():
    from core.kb.external_provider import wiki_module

    module = wiki_module()
    if module is None:
        raise WikiUnsupportedError("当前知识库后端不支持 LLM Wiki")
    return module


def wiki_supported() -> bool:
    """当前后端是否提供 Wiki 能力（决定工具是否暴露）。"""
    try:
        from core.kb.external_provider import supports_wiki

        return bool(supports_wiki())
    except Exception:
        return False


def resolve_dataset_id(dataset_id: str, allowed_dataset_ids: Optional[str]) -> str:
    """解析目标知识库 ID。

    模型多数时候不会填 dataset_id，所以留空时要能自动挑一个：优先取本次请求
    白名单里的第一个，否则从后端列出的库里挑第一个开了 wiki 的。
    """
    explicit = (dataset_id or "").strip()
    if explicit:
        return explicit

    allowed = [x.strip() for x in (allowed_dataset_ids or "").split(",") if x.strip()]
    if allowed:
        return allowed[0]

    try:
        from core.kb.external_provider import list_collections

        for item in list_collections() or []:
            caps = item.get("capabilities") or {}
            if caps.get("wiki"):
                return str(item.get("id") or "")
    except Exception as exc:
        _logger.warning("resolve_dataset_id fallback failed: %s", exc)
    return ""


def _clip(text: Any, limit: int = _SUMMARY_MAX_CHARS) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


def _locate_entry(page: Dict[str, Any]) -> Dict[str, Any]:
    """定位结果条目——只给判断相关性所需的信息，不给正文。"""
    return {
        "slug": page.get("slug"),
        "title": page.get("title"),
        "type": page.get("type_label") or page.get("page_type"),
        "summary": _clip(page.get("summary")),
        "aliases": page.get("aliases") or [],
        "path": page.get("wiki_path") or "",
        # 供模型判断「值不值得展开」和「有没有原文可回溯」
        "related_count": len(page.get("out_links") or []),
        "source_doc_count": len(page.get("source_refs") or []),
    }


def wiki_locate(
    query: str,
    dataset_id: str = "",
    limit: int = 8,
    *,
    allowed_dataset_ids: Optional[str] = None,
) -> Dict[str, Any]:
    """① 在知识库地图上定位：按词命中概念页 / 实体页。"""
    kb_id = resolve_dataset_id(dataset_id, allowed_dataset_ids)
    if not kb_id:
        return {"pages": [], "error": {"code": "no_dataset", "message": "未找到可用的知识库"}}

    result = _wiki_module().wiki_search(kb_id, query, limit=max(1, min(limit, 30)))
    pages = [_locate_entry(p) for p in result.get("pages") or []]
    return {
        "dataset_id": kb_id,
        "query": query,
        "pages": pages,
        "total": len(pages),
        "hint": (
            "命中为空时：换更书面的术语重试，或用正则交替一次给多个说法"
            "（如 资质|牌照|证书）；仍为空则改用 retrieve_dataset_content 走原文语义检索。"
        ),
    }


def wiki_read_page(
    slug: str,
    dataset_id: str = "",
    *,
    allowed_dataset_ids: Optional[str] = None,
) -> Dict[str, Any]:
    """读取单个 Wiki 页面的完整内容与关系。"""
    kb_id = resolve_dataset_id(dataset_id, allowed_dataset_ids)
    if not kb_id:
        return {"error": {"code": "no_dataset", "message": "未找到可用的知识库"}}

    page = _wiki_module().wiki_read_page(kb_id, slug)
    if not page:
        return {"error": {"code": "not_found", "message": f"Wiki 页面不存在: {slug}"}}
    return {
        "dataset_id": kb_id,
        "slug": page.get("slug"),
        "title": page.get("title"),
        "type": page.get("type_label") or page.get("page_type"),
        "path": page.get("wiki_path") or "",
        "content": page.get("content") or "",
        # 已解析成 [{slug,title,page_type}]，模型能直接读懂而不是一串拼音 slug
        "related_pages": page.get("related_pages") or [],
        "referenced_by": page.get("in_links") or [],
        "has_source": bool(page.get("source_refs")),
        "hint": "正文里的 [[slug|显示名]] 是可跳转的其他 Wiki 页；要原文请调 wiki_fetch_source。",
    }


def wiki_expand(
    slug: str,
    dataset_id: str = "",
    depth: int = 1,
    limit: int = 30,
    *,
    allowed_dataset_ids: Optional[str] = None,
) -> Dict[str, Any]:
    """② 沿概念关系展开：把与某页直接/间接相关的概念一次拉齐。"""
    kb_id = resolve_dataset_id(dataset_id, allowed_dataset_ids)
    if not kb_id:
        return {"nodes": [], "error": {"code": "no_dataset", "message": "未找到可用的知识库"}}

    graph = _wiki_module().wiki_graph(
        kb_id,
        mode="ego",
        center=slug,
        depth=max(1, min(depth, 3)),
        limit=max(1, min(limit, 100)),
    )
    nodes = [
        {
            "slug": n.get("slug"),
            "title": n.get("title"),
            "type": n.get("page_type"),
            "link_count": n.get("link_count"),
        }
        for n in graph.get("nodes") or []
    ]
    meta = graph.get("meta") or {}
    return {
        "dataset_id": kb_id,
        "center": slug,
        "nodes": nodes,
        "edges": graph.get("edges") or [],
        "truncated": bool(meta.get("truncated")),
        "hint": "聚合型问题（一共几类 / 彼此什么依赖）用这一步把范围找齐，再逐个取原文。",
    }


def wiki_fetch_source(
    slug: str,
    dataset_id: str = "",
    max_chunks: int = 6,
    *,
    allowed_dataset_ids: Optional[str] = None,
) -> Dict[str, Any]:
    """③ 顺血缘取回原文分块——按 ID 直取，不是再检索一次。"""
    kb_id = resolve_dataset_id(dataset_id, allowed_dataset_ids)
    if not kb_id:
        return {"items": [], "error": {"code": "no_dataset", "message": "未找到可用的知识库"}}

    result = _wiki_module().wiki_source_chunks(
        kb_id, slug, max_chunks=max(1, min(max_chunks, _MAX_SOURCE_CHUNKS))
    )
    page = result.get("page") or {}
    if not page:
        return {"items": [], "error": {"code": "not_found", "message": f"Wiki 页面不存在: {slug}"}}

    items: List[Dict[str, Any]] = []
    for chunk in result.get("chunks") or []:
        items.append(
            {
                "文件名称": chunk.get("document_title") or page.get("title") or "",
                "文件内容": chunk.get("content") or "",
                "document_id": chunk.get("document_id"),
                "chunk_id": chunk.get("chunk_id"),
                "from_wiki_page": page.get("title"),
            }
        )
    return {
        "dataset_id": kb_id,
        "wiki_page": page.get("title"),
        "items": items,
        "total": len(items),
    }


def wiki_overview(
    dataset_id: str = "",
    limit: int = 20,
    *,
    allowed_dataset_ids: Optional[str] = None,
) -> Dict[str, Any]:
    """知识库地图总览：规模统计 + 连接度最高的枢纽概念。"""
    kb_id = resolve_dataset_id(dataset_id, allowed_dataset_ids)
    if not kb_id:
        return {"error": {"code": "no_dataset", "message": "未找到可用的知识库"}}

    module = _wiki_module()
    stats = module.wiki_stats(kb_id)
    graph = module.wiki_graph(kb_id, mode="overview", limit=max(1, min(limit, 60)))
    hubs = [
        {
            "slug": n.get("slug"),
            "title": n.get("title"),
            "type": n.get("page_type"),
            "link_count": n.get("link_count"),
        }
        for n in graph.get("nodes") or []
    ]
    return {
        "dataset_id": kb_id,
        "total_pages": stats.get("total_pages", 0),
        "pages_by_type": stats.get("pages_by_type", {}),
        "total_links": stats.get("total_links", 0),
        "hub_pages": hubs,
        "hint": "枢纽概念是这个库的主干；不确定从哪查起时先看这里，再用 wiki_locate 精确定位。",
    }
