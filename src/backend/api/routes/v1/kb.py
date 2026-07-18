"""Knowledge Base management API routes (v1).

Route definitions only — models live in ``kb_models.py``, processing logic
in ``core/kb_processing.py``, and CRUD in ``core/services/kb_service.py``.
"""

import hashlib
import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query, UploadFile, File, Form, status
from sqlalchemy.orm import Session

from api.routes.v1.kb_models import (
    CreateKBSpaceRequest,
    PolishKBDescriptionRequest,
    ReindexRequest,
    UpdateKBSpaceRequest,
    UpdateChunkRequest,
)
from core.auth.backend import get_current_user, UserContext
from core.db.engine import get_db
from core.infra.exceptions import (
    AccessDeniedError,
    BadRequestError,
    FileTooLargeError,
    ResourceNotFoundError,
    StorageError,
)
from core.content.file_validation import validate_kb_file
from core.content.kb_processing import update_document_status, vectorise_document_background
from core.infra.responses import created_response, paginated_response, success_response
from core.llm.chat_models import get_summarize_model
from core.llm.message_compat import extract_text_from_chat_response, strip_thinking
from core.services import KBService
from core.storage import generate_storage_key, get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/catalog/kb", tags=["Knowledge Base"])

# Constants
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB
MAX_PREVIEW_SIZE = 20 * 1024 * 1024  # 20MB


def _can_read_space(db, space, user_id: str) -> bool:
    """Read permission: owner / everyone for public KBs / grantees of a scoped-visibility KB (single-source-of-truth resolution)."""
    from core.auth.kb_permissions import resolve_local_kb_level
    return resolve_local_kb_level(db, user_id, space.kb_id) != "none"


def _require_kb_write(db, space, user_id: str, required: str) -> None:
    """Write permission check: required is 'edit' (upload/reindex) or 'admin' (rename/delete/edit chunks).

    Raises AccessDeniedError when insufficient. The owner is always admin, superadmins are always admin, granted users go by their level.
    """
    from core.auth.kb_permissions import has_kb_permission, resolve_local_kb_level
    level = resolve_local_kb_level(db, user_id, space.kb_id)
    if not has_kb_permission(level, required):
        raise AccessDeniedError(message="Access denied", reason=f"requires kb level >= {required}")


def _fallback_kb_description(name: str, description: str) -> str:
    base_name = (name or "").strip()
    base_desc = " ".join((description or "").split())
    if base_desc:
        return (
            f"{base_name}相关资料，适合检索概念说明、政策信息、行业动态、业务规则和常见问题。"
            f"当前范围包括：{base_desc[:120]}"
        )[:220]
    return (
        f"{base_name}相关内容，适合检索概念说明、政策解读、业务规则、行业动态、"
        f"典型案例和常见问题。"
    )[:220]


async def _generate_kb_description(name: str, description: str) -> str:
    prompt = f"""/no_think
你是一名知识库配置助手。请根据知识库名称和现有简介，生成一段更利于智能路由判断的“知识库简介”。

目标：
1. 帮助系统快速判断什么问题应该路由到这个知识库
2. 文本要像知识库简介，不是宣传文案
3. 明确主题范围、文档类型、适合回答的问题类型
4. 用中文，1 段即可，控制在 60 到 120 字
5. 不要使用“本知识库可以帮助你”这类口语化表达
6. 不要使用“该知识库聚焦”“本知识库涵盖”这类空泛起手式
7. 直接输出简介正文，不要加标题、引号、序号或解释

知识库名称：{name}
现有简介：{description or "无"}
"""

    try:
        model = get_summarize_model()
        # AgentScope 2.0: model.__call__ takes list[Msg] (not list[dict])
        from agentscope.message import Msg, TextBlock
        result = await model(messages=[Msg(
            name="user", role="user",
            content=[TextBlock(type="text", text=prompt)])])
        polished = strip_thinking(extract_text_from_chat_response(result)).strip()
        polished = " ".join(polished.split())
        return polished or _fallback_kb_description(name, description)
    except Exception as exc:
        logger.warning("Failed to polish KB description for %s: %s", name, exc)
        return _fallback_kb_description(name, description)


# ── Routes ──────────────────────────────────────────────────────────────────

@router.post("/preview-chunks", summary="预览文档分块效果")
async def preview_chunks(
    file: UploadFile = File(..., description="Document file to preview"),
    chunk_method: str = Form("structured", description="Chunking method: structured|recursive|embedding_semantic|laws|qa"),
    parent_chunk_size: int = Form(1024, description="Parent chunk size in tokens"),
    child_chunk_size: int = Form(128, description="Child chunk size in tokens"),
    overlap_tokens: int = Form(20, description="Overlap between child chunks"),
    parent_child_indexing: bool = Form(True, description="Enable parent-child indexing; False = flat chunks only"),
    separators: Optional[str] = Form(None, description="父分块分隔符，JSON 数组字符串；为空用默认层级"),
    child_separators: Optional[str] = Form(None, description="子分块分隔符，JSON 数组字符串；为空走定长滑窗"),
    user: UserContext = Depends(get_current_user),
):
    """预览文档的分块效果，不落库、不写入向量库。上传文件并按指定分块方法（structured/recursive/embedding_semantic/laws/qa）与父子分块参数解析，返回父块及其子块预览，用于创建知识库前调参。文件大小受 MAX_PREVIEW_SIZE（20MB）限制。"""
    content = await file.read()
    if len(content) > MAX_PREVIEW_SIZE:
        raise FileTooLargeError(max_size=MAX_PREVIEW_SIZE, actual_size=len(content))
    if len(content) == 0:
        raise BadRequestError(message="File is empty", data={"filename": file.filename})

    if not file.filename:
        raise BadRequestError(message="Filename is required", data={"field": "file"})
    _, mime_type = validate_kb_file(file.filename, content)

    from core.kb.kb_parser import parse_and_chunk, parse_separators_form, _count_tokens

    _separators = parse_separators_form(separators)
    _child_separators = parse_separators_form(child_separators)

    embed_fn = None
    if chunk_method == "embedding_semantic":
        from core.kb.kb_vector import embed_batch
        embed_fn = embed_batch

    parent_chunks = parse_and_chunk(
        content, mime_type,
        chunk_method=chunk_method,
        parent_size=parent_chunk_size,
        child_size=child_chunk_size,
        overlap=overlap_tokens,
        embed_fn=embed_fn,
        separators=_separators,
        child_separators=_child_separators,
    )

    total_children = sum(len(pc.children) for pc in parent_chunks) if parent_child_indexing else 0

    chunks_preview = []
    for idx, pc in enumerate(parent_chunks):
        if parent_child_indexing:
            children_preview = [
                {"index": c.index, "content": c.content[:200] + ("..." if len(c.content) > 200 else "")}
                for c in pc.children[:5]
            ]
            children_count = len(pc.children)
        else:
            children_preview = []
            children_count = 0
        chunks_preview.append({
            "index": idx,
            "content": pc.content,
            "token_count": _count_tokens(pc.content),
            "children_count": children_count,
            "children_preview": children_preview,
        })

    return success_response(
        data={
            "total_chunks": len(parent_chunks),
            "total_children": total_children,
            "chunks": chunks_preview,
        },
        message="Chunk preview generated successfully",
    )


@router.post("", status_code=status.HTTP_201_CREATED, summary="创建知识库空间")
async def create_kb_space(
    request: CreateKBSpaceRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """创建知识库空间，归属当前登录用户。

    visibility=private 需 ``can_create_private_kb``（仅本人可见）；visibility=public 需
    ``can_create_public_kb``——公有库默认对所有人不可见，创建后**自动授权给创建者所属的
    所有团队（view）**，即对其团队可见；之后可在「用户管理 / 团队管理」继续调整授权。
    """
    visibility = (request.visibility or "private").strip()
    if visibility not in ("private", "public"):
        raise BadRequestError(message="visibility 仅支持 private / public", data={"field": "visibility"})

    # Permission check: require the creation permission matching the chosen visibility
    from core.auth.capabilities import resolve_user_capabilities
    caps = resolve_user_capabilities(db, user.user_id)
    required_cap = "can_create_public_kb" if visibility == "public" else "can_create_private_kb"
    if not caps.get(required_cap, False):
        label = "公有" if visibility == "public" else "私有"
        raise AccessDeniedError(message="Access denied", reason=f"缺少「创建{label}知识库」权限")

    kb_service = KBService(db)
    metadata = dict(request.metadata or {})
    if request.indexing_config:
        metadata["indexing_config"] = request.indexing_config.model_dump()

    # Public KB -> automatically grant visibility to the creator's teams (members inherit view); private KB is owner-only.
    grant_team_ids: list[str] = []
    if visibility == "public":
        from core.auth.kb_permissions import _user_team_ids
        grant_team_ids = sorted(_user_team_ids(db, user.user_id))

    space = kb_service.create_space(
        user_id=user.user_id,
        name=request.name,
        description=request.description,
        chunk_method=request.chunk_method or "semantic",
        metadata=metadata,
        visibility=visibility,
        grant_team_ids=grant_team_ids,
        granted_by=user.user_id,
    )
    return created_response(data=space, message="Knowledge base space created successfully")


@router.post("/polish-description", summary="AI 生成知识库简介")
async def polish_kb_description(
    request: PolishKBDescriptionRequest,
    user: UserContext = Depends(get_current_user),
):
    """根据知识库名称和现有简介，调用大模型生成一段更利于智能路由判断的知识库简介。名称为必填，模型失败时回退到本地模板生成。仅返回润色后的简介文本，不修改任何知识库记录。"""
    name = request.name.strip()
    description = (request.description or "").strip()
    if not name:
        raise BadRequestError(message="Knowledge base name is required", data={"field": "name"})

    polished = await _generate_kb_description(name, description)
    return success_response(
        data={"description": polished},
        message="Knowledge base description polished successfully",
    )


@router.patch("/{kb_id}", summary="更新知识库空间")
async def update_kb_space(
    kb_id: str,
    request: UpdateKBSpaceRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新指定知识库空间的名称与简介。仅空间所有者可操作；空间不存在或无权访问时返回 404。"""
    kb_service = KBService(db)
    space = kb_service.update_space(
        kb_id=kb_id,
        user_id=user.user_id,
        name=request.name,
        description=request.description,
    )
    if not space:
        raise ResourceNotFoundError(resource_type="kb_space", resource_id=kb_id)
    return success_response(data=space, message="Knowledge base space updated successfully")


@router.post("/{kb_id}/documents", status_code=status.HTTP_201_CREATED, summary="上传文档到知识库")
async def upload_document(
    kb_id: str = Path(..., description="KB space ID"),
    file: UploadFile = File(..., description="Document file to upload"),
    title: Optional[str] = Form(None, description="Document title (defaults to filename)"),
    metadata: Optional[str] = Form(None, description="Additional metadata as JSON string"),
    indexing_config: Optional[str] = Form(None, description="Indexing config as JSON string"),
    chunk_method: Optional[str] = Form(None, description="Chunking method override"),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """上传文档到指定知识库空间并异步建立索引。仅空间所有者可上传；文件大小受 MAX_FILE_SIZE（100MB）限制，可选传入标题、元数据、索引配置及分块方法覆盖。上传成功后通过后台任务完成解析、分块与向量化，立即返回 indexing_status=processing。"""
    kb_service = KBService(db)

    from core.db.repository import KBRepository
    kb_repo = KBRepository(db)
    kb_space = kb_repo.get_space(kb_id)

    if not kb_space:
        raise ResourceNotFoundError(resource_type="kb_space", resource_id=kb_id)
    _require_kb_write(db, kb_space, user.user_id, "edit")

    if not file.filename:
        raise BadRequestError(message="Filename is required", data={"field": "file"})

    content = await file.read()
    file_size = len(content)

    if file_size > MAX_FILE_SIZE:
        raise FileTooLargeError(max_size=MAX_FILE_SIZE, actual_size=file_size)
    if file_size == 0:
        raise BadRequestError(message="File is empty", data={"filename": file.filename})

    _, mime_type = validate_kb_file(file.filename, content)

    checksum = hashlib.sha256(content).hexdigest()
    env = os.getenv("ENVIRONMENT", "dev")
    storage_key = generate_storage_key(env=env, user_id=user.user_id, category="kb_documents", filename=file.filename)

    try:
        storage = get_storage()
        storage.upload_bytes(content, storage_key)
        logger.info("Document uploaded to storage: %s", storage_key)
    except StorageError as e:
        logger.error("Failed to upload document to storage: %s", e)
        raise StorageError(operation="upload", error=f"Failed to upload document: {e.data.get('error', str(e))}")

    doc_title = title or file.filename

    if metadata:
        try:
            json.loads(metadata)
        except json.JSONDecodeError:
            raise BadRequestError(message="Invalid metadata JSON", data={"field": "metadata"})

    try:
        document = kb_service.upload_document(
            kb_id=kb_id, user_id=user.user_id, title=doc_title,
            filename=file.filename, size_bytes=file_size, mime_type=mime_type,
            storage_key=storage_key, checksum=checksum,
        )
    except PermissionError as e:
        raise AccessDeniedError(message="Access denied", reason=str(e))

    _effective_chunk_method = chunk_method or kb_space.chunk_method or "semantic"
    _upload_indexing_config = None
    if indexing_config:
        try:
            _upload_indexing_config = json.loads(indexing_config)
        except json.JSONDecodeError:
            raise BadRequestError(message="Invalid indexing_config JSON", data={"field": "indexing_config"})
    _space_extra = kb_space.extra_data if isinstance(kb_space.extra_data, dict) else {}
    _indexing_config = _upload_indexing_config or _space_extra.get("indexing_config")

    background_tasks.add_task(
        vectorise_document_background,
        document_id=document["document_id"],
        kb_id=kb_id, user_id=user.user_id, title=doc_title,
        file_bytes=content, mime_type=mime_type,
        chunk_method=_effective_chunk_method,
        db_url=os.getenv("DATABASE_URL", ""),
        indexing_config=_indexing_config,
    )

    return created_response(
        data={**document, "indexing_status": "processing"},
        message="Document uploaded successfully. Indexing in progress.",
    )


@router.get("/{kb_id}/documents", summary="获取知识库文档列表")
async def list_documents(
    kb_id: str = Path(..., description="KB space ID or Dify dataset ID"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """分页获取指定知识库空间下的文档列表。仅空间所有者可查看本地知识库；当 kb_id 不是本地空间且启用了 Dify 时，回退到 Dify 数据集获取文档列表。"""
    from core.db.repository import KBRepository
    kb_repo = KBRepository(db)
    kb_space = kb_repo.get_space(kb_id)

    if not kb_space:
        from core.kb.dify_kb import is_dify_enabled, list_documents as dify_list_docs
        if is_dify_enabled():
            result = dify_list_docs(kb_id, page=page, limit=page_size)
            return paginated_response(
                items=result.get("items", []),
                page=result.get("page", page),
                page_size=result.get("page_size", page_size),
                total_items=result.get("total", 0),
                message="Documents retrieved successfully",
            )
        raise ResourceNotFoundError(resource_type="kb_space", resource_id=kb_id)

    # Public KBs (created in the admin console, owned by the system principal) are readable by all logged-in users
    if not _can_read_space(db, kb_space, user.user_id):
        raise AccessDeniedError(message="Access denied", reason="Only the KB space owner can list documents")

    documents, total = kb_repo.list_documents(kb_id, page, page_size)
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
    return paginated_response(items=items, page=page, page_size=page_size, total_items=total, message="Documents retrieved successfully")


@router.get("/{kb_id}/documents/{document_id}", summary="获取知识库文档详情")
async def get_document_detail(
    kb_id: str = Path(..., description="KB space ID or Dify dataset ID"),
    document_id: str = Path(..., description="Document ID"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取指定文档的详情，包括标题、文件名、类型及拼接后的全文内容（按分块顺序合并）。仅空间所有者可查看本地知识库；当 kb_id 不是本地空间且启用了 Dify 时，回退到 Dify 获取文档详情。"""
    from core.db.repository import KBRepository
    kb_repo = KBRepository(db)
    kb_space = kb_repo.get_space(kb_id)

    if not kb_space:
        from core.kb.dify_kb import is_dify_enabled, get_document_detail as dify_get_document_detail
        if is_dify_enabled():
            detail = dify_get_document_detail(kb_id, document_id)
            return success_response(data=detail, message="Document detail retrieved successfully")
        raise ResourceNotFoundError(resource_type="kb_space", resource_id=kb_id)

    # Public KBs are readable by all logged-in users
    if not _can_read_space(db, kb_space, user.user_id):
        raise AccessDeniedError(message="Access denied", reason="Only the KB space owner can view document details")

    document = kb_repo.get_document(document_id)
    if not document or document.kb_id != kb_id:
        raise ResourceNotFoundError(resource_type="kb_document", resource_id=document_id)

    from core.db.models import KBChunk
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


@router.delete("/{kb_id}", summary="删除知识库空间")
async def delete_kb_space(
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
async def delete_kb_document(
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
async def reindex_document(
    kb_id: str = Path(..., description="KB space ID"),
    document_id: str = Path(..., description="Document ID"),
    request: Optional[ReindexRequest] = None,
    background_tasks: BackgroundTasks = BackgroundTasks(),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """对指定文档重新建立索引。会先清除该文档的旧分块及向量数据，再从存储重新下载原文，按可选的索引配置与分块方法异步重新解析、分块、向量化。仅空间所有者可操作，立即返回 indexing_status=processing。"""
    from core.db.repository import KBRepository
    from core.db.models import KBChunk

    kb_repo = KBRepository(db)
    kb_space = kb_repo.get_space(kb_id)
    if not kb_space:
        raise ResourceNotFoundError(resource_type="kb_space", resource_id=kb_id)
    _require_kb_write(db, kb_space, user.user_id, "edit")

    document = kb_repo.get_document(document_id)
    if not document or document.kb_id != kb_id:
        raise ResourceNotFoundError(resource_type="kb_document", resource_id=document_id)

    db.query(KBChunk).filter(KBChunk.document_id == document_id, KBChunk.kb_id == kb_id).delete()
    document.indexing_status = "processing"
    db.commit()

    try:
        from core.kb.kb_vector import delete_by_document
        delete_by_document(document_id, user.user_id)
    except Exception as exc:
        logger.warning("Milvus cleanup failed for reindex of %s: %s", document_id, exc)

    try:
        storage = get_storage()
        file_bytes = storage.download_bytes(document.storage_key)
    except Exception as exc:
        update_document_status(document_id, "failed")
        raise BadRequestError(message=f"Failed to retrieve document from storage: {exc}", data={"document_id": document_id})

    _idx_cfg = None
    if request and request.indexing_config:
        _idx_cfg = request.indexing_config.model_dump()
    if not _idx_cfg:
        _space_extra = kb_space.extra_data if isinstance(kb_space.extra_data, dict) else {}
        _idx_cfg = _space_extra.get("indexing_config")

    _reindex_chunk_method = (request.chunk_method if request and request.chunk_method else None) or kb_space.chunk_method or "semantic"

    background_tasks.add_task(
        vectorise_document_background,
        document_id=document_id, kb_id=kb_id, user_id=user.user_id,
        title=document.title, file_bytes=file_bytes, mime_type=document.mime_type,
        chunk_method=_reindex_chunk_method, db_url=os.getenv("DATABASE_URL", ""),
        indexing_config=_idx_cfg,
    )

    return success_response(
        data={"document_id": document_id, "indexing_status": "processing"},
        message="Document re-indexing started",
    )


@router.get("/{kb_id}/chunks", summary="获取文档分块列表（含标签和问题）")
async def list_chunks(
    kb_id: str = Path(..., description="KB space ID"),
    document_id: Optional[str] = Query(None, description="Filter by document ID"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """分页获取知识库分块列表，可选按 document_id 过滤。每个分块返回完整正文内容、标签和问题列表，供分块管理界面查看与编辑。仅空间所有者可访问。"""
    from core.db.repository import KBRepository
    from core.db.models import KBChunk

    kb_repo = KBRepository(db)
    kb_space = kb_repo.get_space(kb_id)
    if not kb_space:
        raise ResourceNotFoundError(resource_type="kb_space", resource_id=kb_id)
    # Public KBs are readable by all logged-in users (chunks are read-only viewing)
    if not _can_read_space(db, kb_space, user.user_id):
        raise AccessDeniedError(message="Access denied", reason="Not the KB owner")

    query = db.query(KBChunk).filter(KBChunk.kb_id == kb_id)
    if document_id:
        query = query.filter(KBChunk.document_id == document_id)
    total = query.count()
    chunks = query.order_by(KBChunk.chunk_index).offset((page - 1) * page_size).limit(page_size).all()

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
async def list_chunk_children(
    kb_id: str = Path(..., description="KB space ID"),
    chunk_id: str = Path(..., description="Parent chunk ID"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回某父块在向量库里的子块（父子分块模式）。扁平模式无子块时返回空列表。仅空间可读者可访问。"""
    from core.db.repository import KBRepository
    from core.db.models import KBChunk

    kb_repo = KBRepository(db)
    kb_space = kb_repo.get_space(kb_id)
    if not kb_space:
        raise ResourceNotFoundError(resource_type="kb_space", resource_id=kb_id)
    if not _can_read_space(db, kb_space, user.user_id):
        raise AccessDeniedError(message="Access denied", reason="Not the KB owner")

    parent = db.query(KBChunk).filter(KBChunk.chunk_id == chunk_id, KBChunk.kb_id == kb_id).first()
    if not parent:
        raise ResourceNotFoundError(resource_type="kb_chunk", resource_id=chunk_id)

    from core.kb.kb_vector import get_children_for_parent
    children = get_children_for_parent(chunk_id)
    return success_response(data={"children": children, "count": len(children)}, message="Chunk children retrieved successfully")


@router.patch("/{kb_id}/chunks/{chunk_id}", summary="更新分块内容/标签/问题")
async def update_chunk(
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
    from core.db.repository import KBRepository
    from core.db.models import KBChunk, KBDocument
    from core.kb.kb_vector import reindex_chunk_content, reindex_chunk_tags, upsert_question_rows

    kb_repo = KBRepository(db)
    kb_space = kb_repo.get_space(kb_id)
    if not kb_space:
        raise ResourceNotFoundError(resource_type="kb_space", resource_id=kb_id)
    _require_kb_write(db, kb_space, user.user_id, "admin")
    if KBService(db)._is_system_managed_space(kb_space):
        raise AccessDeniedError(message="Access denied", reason="System managed KB does not allow chunk edits")

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
                chunk_id=chunk_id, content=chunk.content, tags=chunk.tags or [],
                user_id=user.user_id, kb_id=kb_id,
                document_id=chunk.document_id, title=doc_title,
                chunk_index=chunk.chunk_index,
            )
        elif request.tags is not None:
            reindex_chunk_tags(chunk_id, chunk.content, chunk.tags)
        if request.questions is not None:
            upsert_question_rows(
                parent_chunk_id=chunk_id, questions=chunk.questions,
                user_id=user.user_id, kb_id=kb_id,
                document_id=chunk.document_id, title=doc_title,
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
