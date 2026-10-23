"""local graph responsibilities."""
from __future__ import annotations
from typing import Any, Dict, List, Sequence
from core.db.engine import SessionLocal
from features.knowledge.models import KBWikiPage
from features.knowledge.models import WIKI_PAGE_TYPE_INDEX
from features.knowledge.wiki.slug import readable_from_slug
from sqlalchemy.orm import Session
from features.knowledge.wiki import local_projection
from features.knowledge.wiki import local_provider_state

def resolve_link_titles(
    db: Session, kb_id: str, slugs: Sequence[str], *, resolve: bool
) -> List[Dict[str, str]]:
    if not slugs:
        return []
    titles: Dict[str, KBWikiPage] = {}
    if resolve:
        rows = local_projection.base_query(db, kb_id).filter(KBWikiPage.slug.in_(list(slugs))).all()
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
    limit = max(1, min(int(limit or 60), local_provider_state.MAX_GRAPH_NODES))
    depth = max(1, min(int(depth or 1), 3))
    wanted = local_projection.split_types(types)

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
        selected = ego_nodes(undirected, center, depth, limit)
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


def ego_nodes(adjacency: Dict[str, set], center: str, depth: int, limit: int) -> set:
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
