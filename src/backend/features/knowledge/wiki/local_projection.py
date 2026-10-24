"""local projection responsibilities."""
from __future__ import annotations
from typing import Any, Dict, List, Sequence
from features.knowledge.models import KBDocument, KBWikiPage
from sqlalchemy.orm import Session
from features.knowledge.wiki import local_provider_state

def split_types(raw: str) -> List[str]:
    return [t.strip() for t in str(raw or "").split(",") if t.strip()]


def slim_page(page: KBWikiPage, *, with_content: bool = False) -> Dict[str, Any]:
    """页面 → 精简结构。

    定位阶段只要标题/摘要/链接/血缘，正文按需带：一页动辄数千字，无差别塞进模型
    上下文会把「先定位再取回」的成本优势整个吃掉。
    """
    item: Dict[str, Any] = {
        "slug": page.slug,
        "title": page.title,
        "page_type": page.page_type,
        "type_label": local_provider_state.PAGE_TYPE_LABELS.get(page.page_type or "", ""),
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


def base_query(db: Session, kb_id: str):
    return db.query(KBWikiPage).filter(KBWikiPage.kb_id == kb_id, KBWikiPage.deleted_at.is_(None))


def document_titles(db: Session, kb_id: str, document_ids: Sequence[str]) -> Dict[str, str]:
    ids = [d for d in dict.fromkeys(document_ids) if d]
    if not ids:
        return {}
    rows = (
        db.query(KBDocument.document_id, KBDocument.title, KBDocument.filename)
        .filter(KBDocument.kb_id == kb_id, KBDocument.document_id.in_(ids))
        .all()
    )
    return {row.document_id: (row.title or row.filename or row.document_id) for row in rows}
