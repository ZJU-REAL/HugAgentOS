"""LLM Wiki / 概念图谱工具的实现层。

这组工具服务于一条**单向链**：

    ① 在地图上定位   wiki_locate   —— 用问题里的词命中概念页 / 实体页
    ② 沿关系展开     wiki_expand   —— 顺着页面的双链把相关概念一次拉齐
    ③ 顺血缘取原文   wiki_fetch_source —— 按 chunk_refs 直取原文，不再检索一次

Wiki 页面是知识库的**地图**，不是第二个检索源，也不进向量库。地图负责把范围
收窄到正确的位置，真正的答案与出处始终来自原文分块——所以 ③ 是按 ID 直取而
不是拿 Wiki 摘要当答案。

**两类知识库都支持**：自建库（``kb_`` 前缀，勾选了 Wiki 索引模式）读本地生成的
页面，外接库透传外部后端的产物。dataset_id 传哪种都行，分发在 wiki_router 里。

**权限**：自建库必须过 ``get_accessible_local_kb_ids`` 判定——Wiki 是原文的衍生物，
可读性要与原文一致，绝不能让这组工具成为绕过知识库授权的旁路。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

_logger = logging.getLogger(__name__)

# 定位阶段每页摘要的截断长度：够模型判断相关性，又不至于把上下文吃满
_SUMMARY_MAX_CHARS = 180
# 单次取回原文分块的硬上限
_MAX_SOURCE_CHUNKS = 20


class WikiUnsupportedError(RuntimeError):
    """该知识库不提供 Wiki / 图谱能力。"""


class WikiAccessDeniedError(RuntimeError):
    """当前用户无权访问该知识库。"""


def _split_ids(raw: Optional[str]) -> List[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def _accessible_local_kb_ids(current_user_id: Optional[str]) -> List[str]:
    """当前用户可见的自建库 ID。拿不到 user_id 时返回空——宁可少给，不可错给。

    环境变量回落与同包的 retrieve 工具保持一致（stdio/本地调试下没有请求头，
    身份只能从 CURRENT_USER_ID 来）；两边不一致会导致同一次调试里 wiki 工具和
    检索工具对"能看到哪些库"给出不同答案。
    """
    user_id = (current_user_id or "").strip() or os.getenv("CURRENT_USER_ID", "").strip()
    if not user_id:
        return []
    try:
        from core.auth.kb_permissions import get_accessible_local_kb_ids
        from core.db.engine import SessionLocal

        with SessionLocal() as db:
            return sorted(get_accessible_local_kb_ids(db, user_id))
    except Exception as exc:  # noqa: BLE001
        _logger.warning("解析可访问的自建知识库失败: %s", exc)
        return []


def _assert_access(
    kb_id: str, *, allowed_kb_ids: Optional[str], current_user_id: Optional[str]
) -> None:
    """自建库要过权限；外接库的白名单由其实现模块把关。"""
    from core.kb.wiki_router import is_local_kb

    if not is_local_kb(kb_id):
        return
    # 请求头带了白名单就以它为准（编排层已按当前用户解析过），否则回库里查
    allowed = _split_ids(allowed_kb_ids) or _accessible_local_kb_ids(current_user_id)
    if kb_id not in allowed:
        raise WikiAccessDeniedError(f"无权访问知识库 {kb_id}")


def _wiki_module(
    kb_id: str,
    *,
    allowed_kb_ids: Optional[str] = None,
    current_user_id: Optional[str] = None,
):
    """校验访问权限并取回该库的 Wiki 实现模块。

    权限校验和模块解析绑在一起，是为了让「先鉴权再取数」成为唯一写法——分开写
    迟早会有某个工具漏掉那一行。
    """
    from core.kb.wiki_router import wiki_module_for

    _assert_access(kb_id, allowed_kb_ids=allowed_kb_ids, current_user_id=current_user_id)
    module = wiki_module_for(kb_id)
    if module is None:
        raise WikiUnsupportedError(f"知识库 {kb_id} 未提供 LLM Wiki")
    return module


def wiki_supported() -> bool:
    """平台上是否存在任何 Wiki 源（决定工具是否暴露在清单里）。"""
    try:
        from core.kb.wiki_router import supports_wiki

        return bool(supports_wiki())
    except Exception:
        return False


def resolve_dataset_id(
    dataset_id: str,
    allowed_dataset_ids: Optional[str],
    *,
    allowed_kb_ids: Optional[str] = None,
    current_user_id: Optional[str] = None,
) -> str:
    """解析目标知识库 ID。

    模型多数时候不会填 dataset_id，所以留空时要能自动挑一个。挑选顺序：本次请求
    的自建库白名单里第一个开了 Wiki 的 → 外接库白名单第一个 → 外接后端里第一个
    开了 wiki 的库。自建库排在前面，因为「用户自己传的资料」通常才是问题所指。
    """
    explicit = (dataset_id or "").strip()
    if explicit:
        return explicit

    from core.kb.wiki_router import wiki_capable_local_kb_ids

    local_candidates = _split_ids(allowed_kb_ids) or _accessible_local_kb_ids(current_user_id)
    wiki_locals = wiki_capable_local_kb_ids(local_candidates)
    if wiki_locals:
        return wiki_locals[0]

    external = _split_ids(allowed_dataset_ids)
    if external:
        return external[0]

    try:
        from core.kb.external_provider import list_collections

        for item in list_collections() or []:
            caps = item.get("capabilities") or {}
            if caps.get("wiki"):
                return str(item.get("id") or "")
    except Exception as exc:
        _logger.warning("resolve_dataset_id fallback failed: %s", exc)
    return ""


def _bind(
    dataset_id: str,
    allowed_dataset_ids: Optional[str],
    allowed_kb_ids: Optional[str],
    current_user_id: Optional[str],
    *,
    empty: Optional[Dict[str, Any]] = None,
):
    """解析目标知识库并取回其 Wiki 实现。

    五个工具的开头完全一样：解析 dataset_id → 解析不出就返回结构化的 no_dataset →
    否则鉴权并取模块。抽出来是为了让「先鉴权再取数」只有一处写法。

    返回 ``(kb_id, module, error)``：``error`` 非 None 时调用方直接把它返回。
    """
    kb_id = resolve_dataset_id(
        dataset_id,
        allowed_dataset_ids,
        allowed_kb_ids=allowed_kb_ids,
        current_user_id=current_user_id,
    )
    if not kb_id:
        return (
            "",
            None,
            {
                **(empty or {}),
                "error": {"code": "no_dataset", "message": "未找到可用的知识库"},
            },
        )
    module = _wiki_module(kb_id, allowed_kb_ids=allowed_kb_ids, current_user_id=current_user_id)
    return kb_id, module, None


def _node_entry(node: Dict[str, Any]) -> Dict[str, Any]:
    """图谱节点 → 工具返回条目。展开与总览两个工具共用。"""
    return {
        "slug": node.get("slug"),
        "title": node.get("title"),
        "type": node.get("page_type"),
        "link_count": node.get("link_count"),
    }


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
    allowed_kb_ids: Optional[str] = None,
    current_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """① 在知识库地图上定位：按词命中概念页 / 实体页。"""
    kb_id, module, error = _bind(
        dataset_id, allowed_dataset_ids, allowed_kb_ids, current_user_id, empty={"pages": []}
    )
    if error:
        return error
    result = module.wiki_search(kb_id, query, limit=max(1, min(limit, 30)))
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
    allowed_kb_ids: Optional[str] = None,
    current_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """读取单个 Wiki 页面的完整内容与关系。"""
    kb_id, module, error = _bind(dataset_id, allowed_dataset_ids, allowed_kb_ids, current_user_id)
    if error:
        return error
    page = module.wiki_read_page(kb_id, slug)
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
    allowed_kb_ids: Optional[str] = None,
    current_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """② 沿概念关系展开：把与某页直接/间接相关的概念一次拉齐。"""
    kb_id, module, error = _bind(
        dataset_id, allowed_dataset_ids, allowed_kb_ids, current_user_id, empty={"nodes": []}
    )
    if error:
        return error
    graph = module.wiki_graph(
        kb_id,
        mode="ego",
        center=slug,
        depth=max(1, min(depth, 3)),
        limit=max(1, min(limit, 100)),
    )
    nodes = [_node_entry(n) for n in graph.get("nodes") or []]
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
    allowed_kb_ids: Optional[str] = None,
    current_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """③ 顺血缘取回原文分块——按 ID 直取，不是再检索一次。"""
    kb_id, module, error = _bind(
        dataset_id, allowed_dataset_ids, allowed_kb_ids, current_user_id, empty={"items": []}
    )
    if error:
        return error
    result = module.wiki_source_chunks(
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
    allowed_kb_ids: Optional[str] = None,
    current_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """知识库地图总览：规模统计 + 连接度最高的枢纽概念。"""
    kb_id, module, error = _bind(dataset_id, allowed_dataset_ids, allowed_kb_ids, current_user_id)
    if error:
        return error
    stats = module.wiki_stats(kb_id)
    graph = module.wiki_graph(kb_id, mode="overview", limit=max(1, min(limit, 60)))
    hubs = [_node_entry(n) for n in graph.get("nodes") or []]
    return {
        "dataset_id": kb_id,
        "total_pages": stats.get("total_pages", 0),
        "pages_by_type": stats.get("pages_by_type", {}),
        "total_links": stats.get("total_links", 0),
        "hub_pages": hubs,
        "hint": "枢纽概念是这个库的主干；不确定从哪查起时先看这里，再用 wiki_locate 精确定位。",
    }
