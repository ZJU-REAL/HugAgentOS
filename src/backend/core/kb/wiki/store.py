"""Wiki 页面与目录的仓储层。

所有对 ``kb_wiki_pages`` / ``kb_wiki_folders`` 的读写都走这里，好让「目录归属是
真源、路径字段是派生缓存」这条不变量只在一个地方维护。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from core.db.models import KBWikiFolder, KBWikiPage
from core.db.models.kb_wiki import (
    WIKI_CATEGORY_MAX_DEPTH,
    WIKI_EDIT_SOURCE_PIPELINE,
    WIKI_PAGE_TYPE_ENTITY,
)
from sqlalchemy import func
from sqlalchemy.orm import Session

# 目录标签里要剔除的「类型词」：模型偶尔会拿页面类型当目录名，那不是分类
_TYPE_LABELS = {
    "entity",
    "concept",
    "summary",
    "index",
    "wiki",
    "实体",
    "概念",
    "摘要",
    "页面",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def clean_category_part(part: Any) -> List[str]:
    """规范化单个目录标签：拆内嵌分隔符、剥引号括号、丢掉类型词。"""
    text = str(part or "").strip()
    if not text:
        return []
    for separator in ("／", "｜", "|"):
        text = text.replace(separator, "/")
    cleaned: List[str] = []
    for raw in text.split("/"):
        label = raw.strip().strip("\"'“”‘’[]（）()").strip()
        if not label or label.lower().rstrip("s") in _TYPE_LABELS:
            continue
        cleaned.append(label)
    return cleaned


def clean_category_path(parts: Optional[Sequence[Any]]) -> List[str]:
    """规范化、去重并按最大层级截断整条路径。

    存路径与查路径都走这个函数，保证目录筛选不会因为两侧规范化方式不同而失灵。
    """
    cleaned: List[str] = []
    for part in parts or []:
        for label in clean_category_part(part):
            if label in cleaned:
                continue
            cleaned.append(label)
            if len(cleaned) >= WIKI_CATEGORY_MAX_DEPTH:
                return cleaned
    return cleaned


# ── 目录 ────────────────────────────────────────────────────────────────────


def walk_folder_path(
    db: Session, kb_id: str, path: Sequence[str], *, create: bool
) -> tuple[Optional[str], List[str], str]:
    """按名称链逐级定位目录，返回 ``(叶子 folder_id, 规范化路径, 物化路径串)``。

    ``create=True`` 时缺失的层级就地补建；``create=False`` 时任一层不存在返回
    ``(None, ...)``。两种走法共用同一个匹配谓词——分成两份实现，迟早会在加了
    软删/租户条件时只改到其中一份。

    空路径表示挂在 Wiki 根下，此时 folder_id 为空串。
    """
    labels = clean_category_path(path)
    if not labels:
        return "", [], ""

    parent_id = ""
    materialized: List[str] = []
    for depth, label in enumerate(labels):
        materialized.append(label)
        folder = (
            db.query(KBWikiFolder)
            .filter(
                KBWikiFolder.kb_id == kb_id,
                KBWikiFolder.parent_id == parent_id,
                KBWikiFolder.name == label,
                KBWikiFolder.deleted_at.is_(None),
            )
            .one_or_none()
        )
        if folder is None:
            if not create:
                return None, labels, ""
            folder = KBWikiFolder(
                folder_id=_new_id("kbwf"),
                kb_id=kb_id,
                parent_id=parent_id,
                name=label,
                path="/".join(materialized),
                depth=depth,
                sort_order=0,
            )
            db.add(folder)
            db.flush()
        parent_id = folder.folder_id
    return parent_id, labels, "/".join(materialized)


def ensure_folder_path(db: Session, kb_id: str, path: Sequence[str]) -> tuple[str, List[str], str]:
    """按名称链取或建目录链，返回 ``(叶子 folder_id, 规范化路径, 物化路径串)``。"""
    folder_id, labels, materialized = walk_folder_path(db, kb_id, path, create=True)
    return folder_id or "", labels, materialized


def list_child_folders(db: Session, kb_id: str, parent_id: str = "") -> List[KBWikiFolder]:
    return (
        db.query(KBWikiFolder)
        .filter(
            KBWikiFolder.kb_id == kb_id,
            KBWikiFolder.parent_id == (parent_id or ""),
            KBWikiFolder.deleted_at.is_(None),
        )
        .order_by(KBWikiFolder.sort_order, KBWikiFolder.name)
        .all()
    )


def descendant_folder_ids(db: Session, kb_id: str, folder_id: str) -> List[str]:
    """某目录及其全部后代的 ID（含自身）。层级最多 3 级，逐层展开即可。"""
    if not folder_id:
        return []
    collected = [folder_id]
    frontier = [folder_id]
    for _ in range(WIKI_CATEGORY_MAX_DEPTH):
        if not frontier:
            break
        rows = (
            db.query(KBWikiFolder.folder_id)
            .filter(
                KBWikiFolder.kb_id == kb_id,
                KBWikiFolder.parent_id.in_(frontier),
                KBWikiFolder.deleted_at.is_(None),
            )
            .all()
        )
        frontier = [r.folder_id for r in rows]
        collected.extend(frontier)
    return collected


def count_pages_in_folders(
    db: Session, kb_id: str, folder_ids: Sequence[str], page_types: Optional[Sequence[str]] = None
) -> int:
    if not folder_ids:
        return 0
    query = db.query(func.count(KBWikiPage.page_id)).filter(
        KBWikiPage.kb_id == kb_id,
        KBWikiPage.folder_id.in_(list(folder_ids)),
        KBWikiPage.deleted_at.is_(None),
    )
    if page_types:
        query = query.filter(KBWikiPage.page_type.in_(list(page_types)))
    return int(query.scalar() or 0)


def load_folder_tree(db: Session, kb_id: str) -> List[KBWikiFolder]:
    """一次取回该库全部目录。目录表很小（层级封顶 3 级），逐层往返不划算。"""
    return (
        db.query(KBWikiFolder)
        .filter(KBWikiFolder.kb_id == kb_id, KBWikiFolder.deleted_at.is_(None))
        .order_by(KBWikiFolder.sort_order, KBWikiFolder.name)
        .all()
    )


def page_counts_by_folder(
    db: Session, kb_id: str, page_types: Optional[Sequence[str]] = None
) -> Dict[str, int]:
    """``{folder_id: 直属页面数}``，一次聚合查询。"""
    query = db.query(KBWikiPage.folder_id, func.count(KBWikiPage.page_id)).filter(
        KBWikiPage.kb_id == kb_id, KBWikiPage.deleted_at.is_(None)
    )
    if page_types:
        query = query.filter(KBWikiPage.page_type.in_(list(page_types)))
    return {
        folder_id or "": int(count) for folder_id, count in query.group_by(KBWikiPage.folder_id)
    }


def children_index(folders: Sequence[KBWikiFolder]) -> Dict[str, List[KBWikiFolder]]:
    """``{parent_id: [子目录]}``，供在内存里遍历整棵树。"""
    index: Dict[str, List[KBWikiFolder]] = {}
    for folder in folders:
        index.setdefault(folder.parent_id or "", []).append(folder)
    return index


def subtree_counts(
    folders: Sequence[KBWikiFolder], direct_counts: Dict[str, int]
) -> Dict[str, int]:
    """把直属页数沿目录树向上累加成**递归**页数，全在内存里做。"""
    children = children_index(folders)
    totals: Dict[str, int] = {}

    def _walk(folder_id: str) -> int:
        if folder_id in totals:
            return totals[folder_id]
        total = direct_counts.get(folder_id, 0)
        for child in children.get(folder_id, ()):
            total += _walk(child.folder_id)
        totals[folder_id] = total
        return total

    for folder in folders:
        _walk(folder.folder_id)
    return totals


def prune_empty_folders(db: Session, kb_id: str) -> int:
    """删掉不含任何页面、也没有子目录的目录。返回删除数量。

    两次聚合查询把目录树和页数拉进内存，再自底向上判定——原来的写法是每个目录
    两次查询、外层再乘以层数，目录一多就是几百次往返，而这张表本来就小到可以整棵
    装进内存。
    """
    folders = load_folder_tree(db, kb_id)
    if not folders:
        return 0
    direct = page_counts_by_folder(db, kb_id)
    totals = subtree_counts(folders, direct)

    # 递归页数为 0 的目录，其整条子树都是空的，可以一次性全部收走
    removed = 0
    for folder in folders:
        if totals.get(folder.folder_id, 0) == 0:
            folder.deleted_at = _now()
            removed += 1
    if removed:
        db.flush()
    return removed


# ── 页面 ────────────────────────────────────────────────────────────────────


def get_page(db: Session, kb_id: str, slug: str) -> Optional[KBWikiPage]:
    return (
        db.query(KBWikiPage)
        .filter(
            KBWikiPage.kb_id == kb_id,
            KBWikiPage.slug == slug,
            KBWikiPage.deleted_at.is_(None),
        )
        .one_or_none()
    )


def get_pages(db: Session, kb_id: str, slugs: Sequence[str]) -> Dict[str, KBWikiPage]:
    if not slugs:
        return {}
    rows = (
        db.query(KBWikiPage)
        .filter(
            KBWikiPage.kb_id == kb_id,
            KBWikiPage.slug.in_(list(slugs)),
            KBWikiPage.deleted_at.is_(None),
        )
        .all()
    )
    return {row.slug: row for row in rows}


def all_pages(
    db: Session, kb_id: str, page_types: Optional[Sequence[str]] = None
) -> List[KBWikiPage]:
    query = db.query(KBWikiPage).filter(KBWikiPage.kb_id == kb_id, KBWikiPage.deleted_at.is_(None))
    if page_types:
        query = query.filter(KBWikiPage.page_type.in_(list(page_types)))
    return query.order_by(KBWikiPage.page_type, KBWikiPage.title).all()


def upsert_page(
    db: Session,
    kb_id: str,
    *,
    slug: str,
    title: str,
    page_type: str = WIKI_PAGE_TYPE_ENTITY,
    content: str = "",
    summary: str = "",
    aliases: Optional[Sequence[str]] = None,
    source_refs: Optional[Sequence[str]] = None,
    chunk_refs: Optional[Sequence[str]] = None,
    out_links: Optional[Sequence[str]] = None,
    category_path: Optional[Sequence[str]] = None,
    edit_source: str = WIKI_EDIT_SOURCE_PIPELINE,
    editor_id: str = "",
) -> KBWikiPage:
    """写入或更新一个页面。

    ``category_path`` 为 None 表示「不动既有归属」——只有第一次归档或显式重排时
    才传值。这样人工整理过的目录不会被后续管线重跑churn掉。
    """
    page = get_page(db, kb_id, slug)
    if page is None:
        page = KBWikiPage(
            page_id=_new_id("kbwp"),
            kb_id=kb_id,
            slug=slug,
            page_type=page_type,
            version=0,
        )
        db.add(page)

    page.title = title or page.title or slug
    page.page_type = page_type or page.page_type
    page.content = content
    page.summary = summary or page.summary or ""
    page.aliases = _merge_unique(page.aliases, aliases)
    page.source_refs = _merge_unique(page.source_refs, source_refs)
    page.chunk_refs = _merge_unique(page.chunk_refs, chunk_refs)
    page.out_links = list(dict.fromkeys(out_links or []))
    page.last_edit_source = edit_source
    page.last_editor_id = editor_id or ""
    page.version = int(page.version or 0) + 1
    page.updated_at = _now()

    if category_path is not None:
        folder_id, labels, materialized = ensure_folder_path(db, kb_id, category_path)
        page.folder_id = folder_id
        page.category_path = labels
        page.wiki_path = materialized
        page.depth = len(labels)

    db.flush()
    return page


def archive_page(db: Session, page: KBWikiPage) -> None:
    """归档（软删）一个页面。反向链接由 finalize 阶段统一清理。"""
    page.status = "archived"
    page.deleted_at = _now()
    db.flush()


def pages_citing_document(db: Session, kb_id: str, document_id: str) -> List[KBWikiPage]:
    """取材自某文档的全部页面。

    ``source_refs`` 是 JSON 数组，跨方言的包含查询写法不统一（PG 有 jsonb 运算符、
    SQLite 没有），这里在 Python 侧过滤——受影响页数量级很小，不值得为它分方言。
    """
    rows = (
        db.query(KBWikiPage)
        .filter(KBWikiPage.kb_id == kb_id, KBWikiPage.deleted_at.is_(None))
        .all()
    )
    return [row for row in rows if document_id in (row.source_refs or [])]


def _merge_unique(existing: Any, incoming: Optional[Iterable[str]]) -> List[str]:
    merged = list(existing or [])
    for item in incoming or []:
        value = str(item).strip()
        if value and value not in merged:
            merged.append(value)
    return merged
