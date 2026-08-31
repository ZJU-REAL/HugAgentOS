"""自建知识库 Wiki 的读取面。

本模块的函数签名与返回结构，与外接知识库的 Wiki 模块**逐字段同构**——这是有意
为之：路由层与 MCP 工具层只需要按 kb_id 选到不同的模块，调用代码一行都不用改，
前端组件更是完全无感。

相比外接透传，这里有一处实质优势：页面与 ``kb_chunks`` 同库，回溯原文是按
chunk_id 直接查，而不是"拉整篇文档的全部分块再本地过滤"。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from core.db.engine import SessionLocal
from core.db.models import KBChunk, KBDocument, KBWikiPage
from core.db.models.kb_wiki import (
    WIKI_KNOWLEDGE_PAGE_TYPES,
    WIKI_PAGE_TYPE_INDEX,
    WIKI_PAGE_TYPE_LABELS,
    WIKI_PAGE_TYPE_SUMMARY,
)
from core.kb.wiki.config import MAX_GRAPH_NODES
from core.kb.wiki.finalize import INDEX_SLUG
from core.kb.wiki.slug import readable_from_slug
from core.kb.wiki.store import (
    children_index,
    clean_category_path,
    descendant_folder_ids,
    get_page,
    load_folder_tree,
    page_counts_by_folder,
    subtree_counts,
    walk_folder_path,
)
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

PAGE_TYPE_LABELS = WIKI_PAGE_TYPE_LABELS
KNOWLEDGE_PAGE_TYPES = WIKI_KNOWLEDGE_PAGE_TYPES

_MAX_GRAPH_NODES = MAX_GRAPH_NODES
_MAX_SEARCH_LIMIT = 50


def _split_types(raw: str) -> List[str]:
    return [t.strip() for t in str(raw or "").split(",") if t.strip()]


def _slim_page(page: KBWikiPage, *, with_content: bool = False) -> Dict[str, Any]:
    """页面 → 精简结构。

    定位阶段只要标题/摘要/链接/血缘，正文按需带：一页动辄数千字，无差别塞进模型
    上下文会把「先定位再取回」的成本优势整个吃掉。
    """
    item: Dict[str, Any] = {
        "slug": page.slug,
        "title": page.title,
        "page_type": page.page_type,
        "type_label": PAGE_TYPE_LABELS.get(page.page_type or "", ""),
        "summary": page.summary or "",
        "aliases": list(page.aliases or []),
        "category_path": list(page.category_path or []),
        "wiki_path": page.wiki_path or "",
        "out_links": list(page.out_links or []),
        "in_links": list(page.in_links or []),
        "source_refs": list(page.source_refs or []),
        "chunk_refs": list(page.chunk_refs or []),
        "updated_at": page.updated_at.isoformat() if page.updated_at else None,
    }
    if with_content:
        item["content"] = page.content or ""
    return item


def _base_query(db: Session, kb_id: str):
    return db.query(KBWikiPage).filter(KBWikiPage.kb_id == kb_id, KBWikiPage.deleted_at.is_(None))


# ── 统计 ────────────────────────────────────────────────────────────────────


def wiki_stats(kb_id: str) -> Dict[str, Any]:
    """规模统计：总页数、分类型页数、链接数、孤儿页数。

    只投影统计需要的三列——生成期间前端每 10 秒轮一次，把每页几 KB 的 ``content``
    拉出来纯属浪费。
    """
    with SessionLocal() as db:
        rows = (
            db.query(KBWikiPage.page_type, KBWikiPage.in_links, KBWikiPage.out_links)
            .filter(KBWikiPage.kb_id == kb_id, KBWikiPage.deleted_at.is_(None))
            .all()
        )
        by_type: Dict[str, int] = {}
        total_links = 0
        orphan = 0
        for page_type, in_links, out_links in rows:
            by_type[page_type] = by_type.get(page_type, 0) + 1
            total_links += len(out_links or [])
            if not (in_links or []) and not (out_links or []):
                orphan += 1
        return {
            "total_pages": len(rows),
            "pages_by_type": by_type,
            "total_links": total_links,
            "orphan_count": orphan,
        }


# ── 页面列表 ────────────────────────────────────────────────────────────────


def wiki_list_pages(
    kb_id: str,
    page: int = 1,
    page_size: int = 50,
    page_type: str = "",
    category_path: str = "",
    category_depth: int = 0,
) -> Dict[str, Any]:
    """分页列出页面。

    ``page_type`` 支持逗号分隔多选（「知识」视图把实体/概念/综述/对比合成一次
    请求，避免按类型扇出）；``category_path`` 把范围收窄到某个目录子树。
    """
    page = max(1, int(page or 1))
    page_size = max(1, int(page_size or 50))
    with SessionLocal() as db:
        query = _base_query(db, kb_id)
        types = _split_types(page_type)
        if types:
            query = query.filter(KBWikiPage.page_type.in_(types))

        labels = clean_category_path([category_path] if category_path else [])
        if labels:
            folder_id, _, _ = walk_folder_path(db, kb_id, labels, create=False)
            if folder_id is None:
                return {"pages": [], "total": 0, "page": page, "page_size": page_size}
            query = query.filter(
                KBWikiPage.folder_id.in_(descendant_folder_ids(db, kb_id, folder_id))
            )

        total = query.count()
        rows = (
            query.order_by(KBWikiPage.sort_order, KBWikiPage.title)
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {
            "pages": [_slim_page(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }


# ── 目录 ────────────────────────────────────────────────────────────────────


def wiki_folders(kb_id: str, parent_id: str = "", page_types: str = "") -> Dict[str, Any]:
    """某一层的子目录，带**递归**页面计数，供前端逐层懒加载。

    两次查询搞定：整棵目录树 + 一次按目录分组的页数聚合，其余在内存里算。原来是
    每个子目录一次子树查询加一次计数，用户逐层展开时会反复付这个代价。
    """
    types = _split_types(page_types)
    with SessionLocal() as db:
        folders = load_folder_tree(db, kb_id)
        if not folders:
            return {"parent_id": parent_id or "", "folders": []}
        totals = subtree_counts(folders, page_counts_by_folder(db, kb_id, types))
        children = children_index(folders)
        items = [
            {
                "id": folder.folder_id,
                "name": folder.name,
                "parent_id": folder.parent_id or "",
                "page_count": totals.get(folder.folder_id, 0),
                "has_children": bool(children.get(folder.folder_id)),
            }
            for folder in children.get(parent_id or "", [])
        ]
        return {"parent_id": parent_id or "", "folders": items}


# ── 索引总览 ────────────────────────────────────────────────────────────────


def wiki_index(kb_id: str, limit: int = 20, types: str = "") -> Dict[str, Any]:
    """索引总览：一段总述 + 按类型分组的条目清单。"""
    limit = max(1, int(limit or 20))
    wanted = _split_types(types)
    with SessionLocal() as db:
        index_page = get_page(db, kb_id, INDEX_SLUG)
        intro = ""
        if index_page:
            intro = str((index_page.page_metadata or {}).get("intro") or "")

        groups = []
        for page_type in [*KNOWLEDGE_PAGE_TYPES, WIKI_PAGE_TYPE_SUMMARY]:
            if wanted and page_type not in wanted:
                continue
            query = _base_query(db, kb_id).filter(KBWikiPage.page_type == page_type)
            total = query.count()
            if not total:
                continue
            rows = query.order_by(KBWikiPage.title).limit(limit).all()
            groups.append(
                {
                    "type": page_type,
                    "type_label": PAGE_TYPE_LABELS.get(page_type, ""),
                    "total": total,
                    "items": [
                        {
                            "slug": row.slug,
                            "title": row.title,
                            "summary": row.summary or "",
                            "wiki_path": row.wiki_path or "",
                        }
                        for row in rows
                    ],
                }
            )
        return {
            "intro": intro,
            "version": index_page.version if index_page else None,
            "groups": groups,
        }


# ── 检索 ────────────────────────────────────────────────────────────────────


def wiki_search(kb_id: str, query: str, limit: int = 10) -> Dict[str, Any]:
    """按标题/别名/摘要/正文命中页面。

    支持 ``资质|牌照`` 这样的交替写法——口语说法常常匹配不上书面术语，一次给多个
    说法能显著提高命中率。交替在 Python 侧拆成多个 LIKE，避免依赖数据库正则。
    """
    limit = max(1, min(int(limit or 10), _MAX_SEARCH_LIMIT))
    terms = [t.strip() for t in re.split(r"[|｜]", str(query or "")) if t.strip()]
    if not terms:
        return {"pages": [], "total": 0}

    with SessionLocal() as db:
        conditions = []
        for term in terms[:8]:
            pattern = f"%{term.lower()}%"
            conditions.append(func.lower(KBWikiPage.title).like(pattern))
            conditions.append(func.lower(KBWikiPage.summary).like(pattern))
            conditions.append(func.lower(KBWikiPage.content).like(pattern))
        rows = (
            _base_query(db, kb_id)
            .filter(KBWikiPage.page_type != WIKI_PAGE_TYPE_INDEX)
            .filter(or_(*conditions))
            .order_by(KBWikiPage.page_type, KBWikiPage.title)
            .limit(limit * 3)
            .all()
        )

        # 别名命中在 SQL 里跨方言不好写，回到 Python 侧补一遍；同时按"命中位置"
        # 排序——标题命中远比正文里出现一次更相关
        lowered_terms = [t.lower() for t in terms]

        def _rank(page: KBWikiPage) -> int:
            title = (page.title or "").lower()
            aliases = " ".join(page.aliases or []).lower()
            summary = (page.summary or "").lower()
            if any(t == title for t in lowered_terms):
                return 0
            if any(t in title for t in lowered_terms):
                return 1
            if any(t in aliases for t in lowered_terms):
                return 2
            if any(t in summary for t in lowered_terms):
                return 3
            return 4

        ranked = sorted(rows, key=lambda p: (_rank(p), p.title or ""))[:limit]
        return {"pages": [_slim_page(p) for p in ranked], "total": len(ranked)}


# ── 单页 ────────────────────────────────────────────────────────────────────


def wiki_read_page(kb_id: str, slug: str, *, resolve_links: bool = True) -> Dict[str, Any]:
    """读取整页（含正文、双链、血缘坐标）。

    ``out_links`` 只有 slug，直接展示就是一串拼音，人和模型都读不出是什么。这里
    顺手把邻居的标题查回来贴上——同库一次 IN 查询就够。
    """
    with SessionLocal() as db:
        page = get_page(db, kb_id, slug)
        if page is None:
            return {}
        item = _slim_page(page, with_content=True)
        item["related_pages"] = _resolve_link_titles(
            db, kb_id, item["out_links"], resolve=resolve_links
        )
        return item


def _resolve_link_titles(
    db: Session, kb_id: str, slugs: Sequence[str], *, resolve: bool
) -> List[Dict[str, str]]:
    if not slugs:
        return []
    titles: Dict[str, KBWikiPage] = {}
    if resolve:
        rows = _base_query(db, kb_id).filter(KBWikiPage.slug.in_(list(slugs))).all()
        titles = {row.slug: row for row in rows}
    resolved = []
    for slug in slugs:
        row = titles.get(slug)
        resolved.append(
            {
                "slug": slug,
                "title": (row.title if row else "") or readable_from_slug(slug),
                "page_type": (row.page_type if row else "") or slug.split("/", 1)[0],
            }
        )
    return resolved


# ── 图谱 ────────────────────────────────────────────────────────────────────


def wiki_graph(
    kb_id: str,
    mode: str = "overview",
    center: str = "",
    depth: int = 1,
    limit: int = 60,
    types: str = "",
) -> Dict[str, Any]:
    """概念图谱。

    ``overview`` 取全局连接度最高的节点，``ego`` 以 center 为中心展开 depth 层。
    大库有数千节点上万条边，前端必须靠这两种模式分批取，不能一次拉全量。
    """
    limit = max(1, min(int(limit or 60), _MAX_GRAPH_NODES))
    depth = max(1, min(int(depth or 1), 3))
    wanted = _split_types(types)

    with SessionLocal() as db:
        # 图谱只用到这四列；页面正文对邻接表毫无用处，却是最大的一列
        query = db.query(
            KBWikiPage.slug,
            KBWikiPage.title,
            KBWikiPage.page_type,
            KBWikiPage.out_links,
        ).filter(
            KBWikiPage.kb_id == kb_id,
            KBWikiPage.deleted_at.is_(None),
            KBWikiPage.page_type != WIKI_PAGE_TYPE_INDEX,
        )
        if wanted:
            query = query.filter(KBWikiPage.page_type.in_(wanted))
        rows = query.all()

    by_slug = {row.slug: row for row in rows}
    adjacency: Dict[str, List[str]] = {
        row.slug: [s for s in (row.out_links or []) if s in by_slug] for row in rows
    }
    # 边是无向展开用的，两个方向都要能走
    undirected: Dict[str, set] = {slug: set(links) for slug, links in adjacency.items()}
    for slug, links in adjacency.items():
        for target in links:
            undirected.setdefault(target, set()).add(slug)

    if mode == "ego":
        selected = _ego_nodes(undirected, center, depth, limit)
        if not selected:
            return {
                "nodes": [],
                "edges": [],
                "meta": {
                    "mode": "ego",
                    "center": center,
                    "depth": depth,
                    "truncated": False,
                    "total": len(rows),
                    "returned": 0,
                    "total_pages": len(rows),
                },
            }
    else:
        ranked = sorted(rows, key=lambda r: len(undirected.get(r.slug, ())), reverse=True)
        selected = {r.slug for r in ranked[:limit]}

    nodes = [
        {
            "slug": slug,
            "title": by_slug[slug].title or readable_from_slug(slug),
            "page_type": by_slug[slug].page_type,
            "link_count": len(undirected.get(slug, ())),
        }
        for slug in selected
        if slug in by_slug
    ]
    edges = []
    seen_edges = set()
    for slug in selected:
        for target in adjacency.get(slug, []):
            if target not in selected:
                continue
            key = tuple(sorted((slug, target)))
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append({"source": slug, "target": target})

    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "mode": mode,
            "center": center,
            "depth": depth if mode == "ego" else None,
            "truncated": len(rows) > len(selected),
            # 前端类型声明读 total/returned；total_pages 是旧键，保留兼容
            "total": len(rows),
            "returned": len(nodes),
            "total_pages": len(rows),
        },
    }


def _ego_nodes(adjacency: Dict[str, set], center: str, depth: int, limit: int) -> set:
    """从 center 出发做 BFS，取 depth 层内的节点。"""
    if not center or center not in adjacency:
        return set()
    selected = {center}
    frontier = {center}
    for _ in range(depth):
        nxt = set()
        for slug in frontier:
            for neighbour in adjacency.get(slug, ()):
                if neighbour not in selected:
                    nxt.add(neighbour)
        if not nxt:
            break
        for slug in sorted(nxt, key=lambda s: len(adjacency.get(s, ())), reverse=True):
            if len(selected) >= limit:
                break
            selected.add(slug)
        frontier = nxt
        if len(selected) >= limit:
            break
    return selected


# ── 顺血缘取回原文 ──────────────────────────────────────────────────────────


def wiki_source_chunks(kb_id: str, slug: str, max_chunks: int = 6) -> Dict[str, Any]:
    """按页面的 ``chunk_refs`` 直取原文分块。

    这是「地图 → 定位 → 顺坐标取回原文」这条链的最后一跳：**按 ID 直取**，不是
    再检索一次。页面与分块同库，一次 IN 查询就够。
    """
    max_chunks = max(1, min(int(max_chunks or 6), 20))
    with SessionLocal() as db:
        page = get_page(db, kb_id, slug)
        if page is None:
            return {"page": {}, "chunks": []}

        item = _slim_page(page, with_content=True)
        item["related_pages"] = _resolve_link_titles(db, kb_id, item["out_links"], resolve=True)

        chunk_ids = list(page.chunk_refs or [])
        rows: List[KBChunk] = []
        if chunk_ids:
            rows = (
                db.query(KBChunk)
                .filter(KBChunk.kb_id == kb_id, KBChunk.chunk_id.in_(chunk_ids))
                .all()
            )
            # 保持页面记录的引用顺序：那是证据的呈现次序，不是数据库的返回次序
            order = {cid: i for i, cid in enumerate(chunk_ids)}
            rows.sort(key=lambda r: order.get(r.chunk_id, 10**6))

        # chunk_refs 对不上（页面被改写过、或文档重新索引换了分块）时，退回该页
        # 主要来源文档的前几块正文——给不出精确证据，至少不要空手而归
        if not rows and (page.source_refs or []):
            rows = (
                db.query(KBChunk)
                .filter(KBChunk.kb_id == kb_id, KBChunk.document_id == page.source_refs[0])
                .order_by(KBChunk.chunk_index)
                .limit(max_chunks)
                .all()
            )

        titles = _document_titles(db, kb_id, [r.document_id for r in rows])
        chunks = [
            {
                "chunk_id": row.chunk_id,
                "document_id": row.document_id,
                "document_title": titles.get(row.document_id, ""),
                "content": row.content or "",
            }
            for row in rows[:max_chunks]
            if (row.content or "").strip()
        ]
        return {"page": item, "chunks": chunks}


def _document_titles(db: Session, kb_id: str, document_ids: Sequence[str]) -> Dict[str, str]:
    ids = [d for d in dict.fromkeys(document_ids) if d]
    if not ids:
        return {}
    rows = (
        db.query(KBDocument.document_id, KBDocument.title, KBDocument.filename)
        .filter(KBDocument.kb_id == kb_id, KBDocument.document_id.in_(ids))
        .all()
    )
    return {row.document_id: (row.title or row.filename or row.document_id) for row in rows}
