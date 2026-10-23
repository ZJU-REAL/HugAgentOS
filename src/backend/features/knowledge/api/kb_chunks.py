"""kb chunks responsibilities."""
from __future__ import annotations
from fastapi import APIRouter
router = APIRouter()
from typing import Optional
from features.knowledge.edition import UpdateChunkRequest
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.infra.exceptions import AccessDeniedError, BadRequestError, ResourceNotFoundError
from core.infra.responses import paginated_response, success_response
from features.knowledge.service import KBService
from fastapi import Depends, Path, Query
from sqlalchemy.orm import Session
import logging
logger = logging.getLogger(__name__)
from features.knowledge.api import kb_helpers

@router.get("/{kb_id}/chunks", summary="获取文档分块列表（含标签和问题）")
def list_chunks(
    kb_id: str = Path(..., description="KB space ID"),
    document_id: Optional[str] = Query(None, description="Filter by document ID"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """分页获取知识库分块列表，可选按 document_id 过滤。每个分块返回完整正文内容、标签和问题列表，供分块管理界面查看与编辑。仅空间所有者可访问。"""
    from features.knowledge.models import KBChunk
    from features.knowledge.repository import KBRepository

    kb_repo = KBRepository(db)
    kb_space = kb_repo.get_space(kb_id)
    if not kb_space:
        raise ResourceNotFoundError(resource_type="kb_space", resource_id=kb_id)
    # Public KBs are readable by all logged-in users (chunks are read-only viewing)
    if not kb_helpers.can_read_space(db, kb_space, user.user_id):
        raise AccessDeniedError(message="Access denied", reason="Not the KB owner")

    query = db.query(KBChunk).filter(KBChunk.kb_id == kb_id)
    if document_id:
        query = query.filter(KBChunk.document_id == document_id)
    total = query.count()
    chunks = (
        query.order_by(KBChunk.chunk_index).offset((page - 1) * page_size).limit(page_size).all()
    )

    items = [
        {
            "chunk_id": c.chunk_id,
            "document_id": c.document_id,
            "chunk_index": c.chunk_index,
            "content": c.content,
            "tags": c.tags or [],
            "questions": c.questions or [],
        }
        for c in chunks
    ]
    return paginated_response(items=items, page=page, page_size=page_size, total_items=total)


@router.get("/{kb_id}/chunks/{chunk_id}/children", summary="获取父块下的子块列表")
def list_chunk_children(
    kb_id: str = Path(..., description="KB space ID"),
    chunk_id: str = Path(..., description="Parent chunk ID"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回某父块在向量库里的子块（父子分块模式）。扁平模式无子块时返回空列表。仅空间可读者可访问。"""
    from features.knowledge.models import KBChunk
    from features.knowledge.repository import KBRepository

    kb_repo = KBRepository(db)
    kb_space = kb_repo.get_space(kb_id)
    if not kb_space:
        raise ResourceNotFoundError(resource_type="kb_space", resource_id=kb_id)
    if not kb_helpers.can_read_space(db, kb_space, user.user_id):
        raise AccessDeniedError(message="Access denied", reason="Not the KB owner")

    parent = db.query(KBChunk).filter(KBChunk.chunk_id == chunk_id, KBChunk.kb_id == kb_id).first()
    if not parent:
        raise ResourceNotFoundError(resource_type="kb_chunk", resource_id=chunk_id)

    from features.knowledge.vector_indexing import get_children_for_parent

    children = get_children_for_parent(chunk_id)
    return success_response(
        data={"children": children, "count": len(children)},
        message="Chunk children retrieved successfully",
    )


@router.patch("/{kb_id}/chunks/{chunk_id}", summary="更新分块内容/标签/问题")
def update_chunk(
    kb_id: str = Path(..., description="KB space ID"),
    chunk_id: str = Path(..., description="Chunk ID"),
    request: UpdateChunkRequest = ...,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新指定分块的正文内容、标签和问题列表，并同步刷新其在向量库中的向量与索引。

    传入 ``content`` 时会对该分块重新向量化（dense + sparse）；仅改标签时只刷新稀疏/标签
    索引；问题始终单独重建问题向量行。仅空间所有者（admin 权限）可操作，系统托管知识库禁止
    编辑分块；仅传入的字段会被更新。"""
    from features.knowledge.models import KBChunk, KBDocument
    from features.knowledge.repository import KBRepository
    from features.knowledge.vector_indexing import reindex_chunk_content, reindex_chunk_tags, upsert_question_rows

    kb_repo = KBRepository(db)
    kb_space = kb_repo.get_space(kb_id)
    if not kb_space:
        raise ResourceNotFoundError(resource_type="kb_space", resource_id=kb_id)
    kb_helpers.require_kb_write(db, kb_space, user.user_id, "admin")
    if KBService(db)._is_system_managed_space(kb_space):
        raise AccessDeniedError(
            message="Access denied", reason="System managed KB does not allow chunk edits"
        )

    chunk = db.query(KBChunk).filter(KBChunk.chunk_id == chunk_id, KBChunk.kb_id == kb_id).first()
    if not chunk:
        raise ResourceNotFoundError(resource_type="kb_chunk", resource_id=chunk_id)

    content_changed = request.content is not None
    if content_changed:
        new_content = request.content.strip()
        if not new_content:
            raise BadRequestError(message="分块内容不能为空", data={"field": "content"})
        chunk.content = new_content
    if request.tags is not None:
        chunk.tags = request.tags
    if request.questions is not None:
        chunk.questions = request.questions
    db.commit()
    db.refresh(chunk)

    try:
        doc = db.query(KBDocument).filter_by(document_id=chunk.document_id).first()
        doc_title = doc.title if doc else ""

        if content_changed:
            # Content changed: re-vectorize the whole chunk (current tags already included)
            reindex_chunk_content(
                chunk_id=chunk_id,
                content=chunk.content,
                tags=chunk.tags or [],
                user_id=user.user_id,
                kb_id=kb_id,
                document_id=chunk.document_id,
                title=doc_title,
                chunk_index=chunk.chunk_index,
            )
        elif request.tags is not None:
            reindex_chunk_tags(chunk_id, chunk.content, chunk.tags)
        if request.questions is not None:
            upsert_question_rows(
                parent_chunk_id=chunk_id,
                questions=chunk.questions,
                user_id=user.user_id,
                kb_id=kb_id,
                document_id=chunk.document_id,
                title=doc_title,
                chunk_index=chunk.chunk_index,
            )
    except Exception as exc:
        logger.warning("Milvus re-index failed for chunk %s: %s", chunk_id, exc)

    return success_response(
        data={
            "chunk_id": chunk.chunk_id,
            "content": chunk.content,
            "tags": chunk.tags,
            "questions": chunk.questions,
        },
        message="Chunk updated successfully",
    )
