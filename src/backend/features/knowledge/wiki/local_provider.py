"""local provider responsibilities."""
from __future__ import annotations
import re
from typing import Any, Dict, List
from core.db.engine import SessionLocal
from features.knowledge.models import KBChunk, KBWikiPage
from features.knowledge.models import WIKI_PAGE_TYPE_INDEX, WIKI_PAGE_TYPE_SUMMARY
from features.knowledge.wiki.finalize import INDEX_SLUG
from features.knowledge.wiki.store import children_index, clean_category_path, descendant_folder_ids, get_page, load_folder_tree, page_counts_by_folder, subtree_counts, walk_folder_path
from sqlalchemy import func, or_
from features.knowledge.wiki import local_graph
from features.knowledge.wiki import local_projection
from features.knowledge.wiki import local_provider_state

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
        query = local_projection.base_query(db, kb_id)
        types = local_projection.split_types(page_type)
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
            "pages": [local_projection.slim_page(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }


def wiki_folders(kb_id: str, parent_id: str = "", page_types: str = "") -> Dict[str, Any]:
    """某一层的子目录，带**递归**页面计数，供前端逐层懒加载。

    两次查询搞定：整棵目录树 + 一次按目录分组的页数聚合，其余在内存里算。原来是
    每个子目录一次子树查询加一次计数，用户逐层展开时会反复付这个代价。
    """
    types = local_projection.split_types(page_types)
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


def wiki_index(kb_id: str, limit: int = 20, types: str = "") -> Dict[str, Any]:
    """索引总览：一段总述 + 按类型分组的条目清单。"""
    limit = max(1, int(limit or 20))
    wanted = local_projection.split_types(types)
    with SessionLocal() as db:
        index_page = get_page(db, kb_id, INDEX_SLUG)
        intro = ""
        if index_page:
            intro = str((index_page.page_metadata or {}).get("intro") or "")

        groups = []
        for page_type in [*local_provider_state.KNOWLEDGE_PAGE_TYPES, WIKI_PAGE_TYPE_SUMMARY]:
            if wanted and page_type not in wanted:
                continue
            query = local_projection.base_query(db, kb_id).filter(KBWikiPage.page_type == page_type)
            total = query.count()
            if not total:
                continue
            rows = query.order_by(KBWikiPage.title).limit(limit).all()
            groups.append(
                {
                    "type": page_type,
                    "type_label": local_provider_state.PAGE_TYPE_LABELS.get(page_type, ""),
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


def wiki_search(kb_id: str, query: str, limit: int = 10) -> Dict[str, Any]:
    """按标题/别名/摘要/正文命中页面。

    支持 ``资质|牌照`` 这样的交替写法——口语说法常常匹配不上书面术语，一次给多个
    说法能显著提高命中率。交替在 Python 侧拆成多个 LIKE，避免依赖数据库正则。
    """
    limit = max(1, min(int(limit or 10), local_provider_state.MAX_SEARCH_LIMIT))
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
            local_projection.base_query(db, kb_id)
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
        return {"pages": [local_projection.slim_page(p) for p in ranked], "total": len(ranked)}


def wiki_read_page(kb_id: str, slug: str, *, resolve_links: bool = True) -> Dict[str, Any]:
    """读取整页（含正文、双链、血缘坐标）。

    ``out_links`` 只有 slug，直接展示就是一串拼音，人和模型都读不出是什么。这里
    顺手把邻居的标题查回来贴上——同库一次 IN 查询就够。
    """
    with SessionLocal() as db:
        page = get_page(db, kb_id, slug)
        if page is None:
            return {}
        item = local_projection.slim_page(page, with_content=True)
        item["related_pages"] = local_graph.resolve_link_titles(
            db, kb_id, item["out_links"], resolve=resolve_links
        )
        return item


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

        item = local_projection.slim_page(page, with_content=True)
        item["related_pages"] = local_graph.resolve_link_titles(db, kb_id, item["out_links"], resolve=True)

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

        titles = local_projection.document_titles(db, kb_id, [r.document_id for r in rows])
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
