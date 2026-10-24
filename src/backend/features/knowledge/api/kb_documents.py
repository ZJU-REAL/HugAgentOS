"""kb documents responsibilities."""
from __future__ import annotations
from fastapi import APIRouter
router = APIRouter()
import hashlib
import json
import os
from typing import Optional
from core.auth.backend import UserContext, get_current_user
from core.files.content.file_validation import validate_kb_file
from features.knowledge.wiki.config import index_modes_of
from core.db.engine import get_db
from core.infra.exceptions import AccessDeniedError, BadRequestError, FileTooLargeError, ResourceNotFoundError, StorageError
from core.infra.responses import created_response, paginated_response, success_response
from features.knowledge.service import KBService
from core.files.storage import generate_storage_key, get_storage
from fastapi import Depends, File, Form, Path, Query, UploadFile, status
from sqlalchemy.orm import Session
import logging
logger = logging.getLogger(__name__)
from features.knowledge.api import kb_helpers
from features.knowledge.api import kb_state

@router.post("/{kb_id}/documents", status_code=status.HTTP_201_CREATED, summary="上传文档到知识库")
def upload_document(
    kb_id: str = Path(..., description="KB space ID"),
    file: UploadFile = File(..., description="Document file to upload"),
    title: Optional[str] = Form(None, description="Document title (defaults to filename)"),
    metadata: Optional[str] = Form(None, description="Additional metadata as JSON string"),
    indexing_config: Optional[str] = Form(None, description="Indexing config as JSON string"),
    chunk_method: Optional[str] = Form(None, description="Chunking method override"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """上传文档到指定知识库空间并异步建立索引。仅空间所有者可上传；文件大小受 MAX_FILE_SIZE（100MB）限制，可选传入标题、元数据、索引配置及分块方法覆盖。上传成功后通过后台任务完成解析、分块与向量化，立即返回 indexing_status=processing。"""
    kb_service = KBService(db)

    from features.knowledge.repository import KBRepository

    kb_repo = KBRepository(db)
    kb_space = kb_repo.get_space(kb_id)

    if not kb_space:
        raise ResourceNotFoundError(resource_type="kb_space", resource_id=kb_id)
    kb_helpers.require_kb_write(db, kb_space, user.user_id, "edit")

    if not file.filename:
        raise BadRequestError(message="Filename is required", data={"field": "file"})

    content = file.file.read()
    file_size = len(content)

    if file_size > kb_state.MAX_FILE_SIZE:
        raise FileTooLargeError(max_size=kb_state.MAX_FILE_SIZE, actual_size=file_size)
    if file_size == 0:
        raise BadRequestError(message="File is empty", data={"filename": file.filename})

    _, mime_type = validate_kb_file(file.filename, content)

    checksum = hashlib.sha256(content).hexdigest()
    env = os.getenv("ENVIRONMENT", "dev")
    storage_key = generate_storage_key(
        env=env, user_id=user.user_id, category="kb_documents", filename=file.filename
    )

    try:
        storage = get_storage()
        # 对象存储是阻塞的网络调用，直接在 async handler 里调会卡住整个事件循环——
        # 批量上传时后端会连 SSE 和其它接口一起停摆。
        storage.upload_bytes(content, storage_key)
        logger.info("Document uploaded to storage: %s", storage_key)
    except StorageError as e:
        logger.error("Failed to upload document to storage: %s", e)
        raise StorageError(
            operation="upload", error=f"Failed to upload document: {e.data.get('error', str(e))}"
        )

    doc_title = title or file.filename

    if metadata:
        try:
            json.loads(metadata)
        except json.JSONDecodeError:
            raise BadRequestError(message="Invalid metadata JSON", data={"field": "metadata"})

    try:
        document = kb_service.upload_document(
            kb_id=kb_id,
            user_id=user.user_id,
            title=doc_title,
            filename=file.filename,
            size_bytes=file_size,
            mime_type=mime_type,
            storage_key=storage_key,
            checksum=checksum,
        )
    except PermissionError as e:
        raise AccessDeniedError(message="Access denied", reason=str(e))

    _effective_chunk_method = chunk_method or kb_space.chunk_method or "semantic"
    _upload_indexing_config = None
    if indexing_config:
        try:
            _upload_indexing_config = json.loads(indexing_config)
        except json.JSONDecodeError:
            raise BadRequestError(
                message="Invalid indexing_config JSON", data={"field": "indexing_config"}
            )
    _space_extra = kb_space.extra_data if isinstance(kb_space.extra_data, dict) else {}
    _indexing_config = _upload_indexing_config or _space_extra.get("indexing_config")

    # 排进持久化索引队列，由常驻 worker 认领执行（见 core/kb/index_worker.py）。
    # 早先这里挂的是 FastAPI BackgroundTasks：同步任务落进 Starlette 的共享请求线程池，
    # 一次多选几十个文件就把池占满，后续请求连 handler 都进不去——前端一直转圈、网关
    # 报 502；而且任务是进程内的，后端一重启就全丢，文档永远停在「索引中」。
    kb_helpers.enqueue_document_indexing(
        db,
        document_id=document["document_id"],
        user_id=user.user_id,
        chunk_method=_effective_chunk_method,
        indexing_config=_indexing_config,
        index_modes=index_modes_of(kb_space),
    )

    return created_response(
        data={**document, "indexing_status": "processing"},
        message="Document uploaded successfully. Indexing in progress.",
    )


@router.get("/{kb_id}/documents", summary="获取知识库文档列表")
def list_documents(
    kb_id: str = Path(..., description="Local or external knowledge collection ID"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    keyword: Optional[str] = Query(None, description="按标题/文件名搜索（全库范围，再分页）"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """分页获取知识库文档；非本地 ID 交由当前版本的外部知识提供方处理。"""
    from features.knowledge.repository import KBRepository

    kb_repo = KBRepository(db)
    kb_space = kb_repo.get_space(kb_id)

    if not kb_space:
        from features.knowledge.edition import is_enabled, list_documents

        if is_enabled():
            result = list_documents(kb_id, page=page, limit=page_size, keyword=keyword or "")
            response = paginated_response(
                items=result.get("items", []),
                page=result.get("page", page),
                page_size=result.get("page_size", page_size),
                total_items=result.get("total", 0),
                message="Documents retrieved successfully",
            )
            from features.knowledge.edition import count_document_statuses

            status_counts = count_document_statuses(
                kb_id,
                keyword=keyword or "",
                initial_result=result,
            )
            if status_counts is not None:
                response["data"]["status_counts"] = status_counts
            return response
        raise ResourceNotFoundError(resource_type="kb_space", resource_id=kb_id)

    # Public KBs (created in the admin console, owned by the system principal) are readable by all logged-in users
    if not kb_helpers.can_read_space(db, kb_space, user.user_id):
        raise AccessDeniedError(
            message="Access denied", reason="Only the KB space owner can list documents"
        )

    documents, total = kb_repo.list_documents(kb_id, page, page_size, keyword=keyword)
    items = [
        {
            "id": d.document_id,
            "title": d.title,
            "desc": d.filename,
            "filename": d.filename,
            "size": d.size_bytes,
            "mime_type": d.mime_type,
            "storage_key": d.storage_key,
            "uploaded_at": d.uploaded_at.isoformat(),
            "indexing_status": getattr(d, "indexing_status", "processing"),
        }
        for d in documents
    ]
    response = paginated_response(
        items=items,
        page=page,
        page_size=page_size,
        total_items=total,
        message="Documents retrieved successfully",
    )
    response["data"]["status_counts"] = kb_repo.count_document_statuses(kb_id, keyword=keyword)
    return response


@router.get("/{kb_id}/documents/{document_id}", summary="获取知识库文档详情")
def get_document_detail(
    kb_id: str = Path(..., description="Local or external knowledge collection ID"),
    document_id: str = Path(..., description="Document ID"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取文档详情；非本地 ID 交由当前版本的外部知识提供方处理。"""
    from features.knowledge.repository import KBRepository

    kb_repo = KBRepository(db)
    kb_space = kb_repo.get_space(kb_id)

    if not kb_space:
        from features.knowledge.edition import get_document_detail, is_enabled

        if is_enabled():
            detail = get_document_detail(kb_id, document_id)
            return success_response(data=detail, message="Document detail retrieved successfully")
        raise ResourceNotFoundError(resource_type="kb_space", resource_id=kb_id)

    # Public KBs are readable by all logged-in users
    if not kb_helpers.can_read_space(db, kb_space, user.user_id):
        raise AccessDeniedError(
            message="Access denied", reason="Only the KB space owner can view document details"
        )

    document = kb_repo.get_document(document_id)
    if not document or document.kb_id != kb_id:
        raise ResourceNotFoundError(resource_type="kb_document", resource_id=document_id)

    from features.knowledge.models import KBChunk

    chunks = (
        db.query(KBChunk)
        .filter(KBChunk.document_id == document_id, KBChunk.kb_id == kb_id)
        .order_by(KBChunk.chunk_index)
        .all()
    )
    content = "\n\n".join(c.content for c in chunks) if chunks else ""

    return success_response(
        data={
            "id": document.document_id,
            "title": document.title,
            "desc": document.filename,
            "filename": document.filename,
            "mime_type": document.mime_type,
            "uploaded_at": document.uploaded_at.isoformat(),
            "content": content,
        },
        message="Document detail retrieved successfully",
    )
