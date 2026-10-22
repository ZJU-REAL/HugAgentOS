"""kb mutations responsibilities."""
from __future__ import annotations
from fastapi import APIRouter
router = APIRouter()
from typing import Optional
from features.knowledge.edition import ReindexRequest
from core.auth.backend import UserContext, get_current_user
from features.knowledge.wiki.config import index_modes_of
from core.db.engine import get_db
from core.infra.exceptions import ResourceNotFoundError
from core.infra.responses import success_response
from features.knowledge.service import KBService
from fastapi import Depends, Path
from sqlalchemy.orm import Session
import logging
logger = logging.getLogger(__name__)
from features.knowledge.api import kb_helpers

@router.delete("/{kb_id}", summary="删除知识库空间")
def delete_kb_space(
    kb_id: str = Path(..., description="KB space ID"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除指定知识库空间及其下属文档、分块与向量数据。仅空间所有者可操作；空间不存在或无权访问时返回 404。此操作不可恢复。"""
    kb_service = KBService(db)
    deleted = kb_service.delete_space(kb_id, user.user_id)
    if not deleted:
        raise ResourceNotFoundError(resource_type="kb_space", resource_id=kb_id)
    return success_response(data=None, message="Knowledge base deleted successfully")


@router.delete("/{kb_id}/documents/{document_id}", summary="删除知识库文档")
def delete_kb_document(
    kb_id: str = Path(..., description="KB space ID"),
    document_id: str = Path(..., description="Document ID"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """从知识库空间删除指定文档及其分块与向量数据。仅文档所属空间的所有者可操作；文档不存在或无权访问时返回 404。此操作不可恢复。"""
    kb_service = KBService(db)
    deleted = kb_service.delete_document(document_id, user.user_id)
    if not deleted:
        raise ResourceNotFoundError(resource_type="kb_document", resource_id=document_id)
    return success_response(data=None, message="Document deleted successfully")


@router.post("/{kb_id}/documents/{document_id}/reindex", summary="重新索引文档")
def reindex_document(
    kb_id: str = Path(..., description="KB space ID"),
    document_id: str = Path(..., description="Document ID"),
    request: Optional[ReindexRequest] = None,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """对指定文档重新建立索引。会先清除该文档的旧分块及向量数据，再从存储重新下载原文，按可选的索引配置与分块方法异步重新解析、分块、向量化。仅空间所有者可操作，立即返回 indexing_status=processing。"""
    from features.knowledge.models import KBChunk
    from features.knowledge.repository import KBRepository

    kb_repo = KBRepository(db)
    kb_space = kb_repo.get_space(kb_id)
    if not kb_space:
        raise ResourceNotFoundError(resource_type="kb_space", resource_id=kb_id)
    kb_helpers.require_kb_write(db, kb_space, user.user_id, "edit")

    document = kb_repo.get_document(document_id)
    if not document or document.kb_id != kb_id:
        raise ResourceNotFoundError(resource_type="kb_document", resource_id=document_id)

    db.query(KBChunk).filter(KBChunk.document_id == document_id, KBChunk.kb_id == kb_id).delete()
    document.indexing_status = "processing"
    db.commit()

    # 资产随分块一起重建：不清会在每次重索引后留下一份孤儿图（存储与表都涨）。
    try:
        from features.knowledge.kb_assets import purge_document_assets

        purge_document_assets(db, document_id)
    except Exception as exc:
        logger.warning("Asset cleanup failed for reindex of %s: %s", document_id, exc)

    try:
        from features.knowledge.vector_indexing import delete_by_document

        delete_by_document(document_id, user.user_id)
    except Exception as exc:
        logger.warning("Milvus cleanup failed for reindex of %s: %s", document_id, exc)

    _idx_cfg = None
    if request and request.indexing_config:
        _idx_cfg = request.indexing_config.model_dump()
    if not _idx_cfg:
        _space_extra = kb_space.extra_data if isinstance(kb_space.extra_data, dict) else {}
        _idx_cfg = _space_extra.get("indexing_config")

    _reindex_chunk_method = (
        (request.chunk_method if request and request.chunk_method else None)
        or kb_space.chunk_method
        or "semantic"
    )

    # 与上传同一条路：排队 + 常驻 worker，原文由 worker 自己从存储取回。
    # 这里不再预先 download_bytes——几十 MB 的原文既没必要在请求里读，也不该跟着
    # 任务在内存里排队等着跑。
    kb_helpers.enqueue_document_indexing(
        db,
        document_id=document_id,
        user_id=user.user_id,
        chunk_method=_reindex_chunk_method,
        indexing_config=_idx_cfg,
        index_modes=index_modes_of(kb_space),
    )

    return success_response(
        data={"document_id": document_id, "indexing_status": "processing"},
        message="Document re-indexing started",
    )
