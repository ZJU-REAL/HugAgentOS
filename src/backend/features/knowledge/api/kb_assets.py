"""kb assets responsibilities."""
from __future__ import annotations
from fastapi import APIRouter
router = APIRouter()
from typing import Optional
from core.auth.backend import UserContext, get_current_user
from core.files.content.file_validation import validate_kb_file
from core.db.engine import get_db
from core.infra.exceptions import BadRequestError, FileTooLargeError, ResourceNotFoundError
from core.infra.responses import success_response
from core.files.storage import get_storage
from fastapi import Depends, File, Form, Path, UploadFile
from sqlalchemy.orm import Session
import logging
logger = logging.getLogger(__name__)
from features.knowledge.api import kb_helpers
from features.knowledge.api import kb_state

@router.get("/assets/{asset_id}", summary="读取知识库媒体资产")
def get_kb_asset(
    asset_id: str = Path(..., description="Asset ID"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """按 ID 读取知识库中的媒体资产（版面解析切出的图片等）原始字节。

    资产的可见性完全跟随所属知识库空间：能读该库才能读它的图。图片链接会出现在分块正文
    与检索结果里，故按 ID 直取，不经过库/文档路径。
    """
    from fastapi.responses import Response

    from features.knowledge.models import KBAsset
    from features.knowledge.repository import KBRepository

    asset = db.query(KBAsset).filter(KBAsset.asset_id == asset_id).first()
    if not asset:
        raise ResourceNotFoundError(resource_type="kb_asset", resource_id=asset_id)

    kb_space = KBRepository(db).get_space(asset.kb_id)
    if not kb_space or not kb_helpers.can_read_space(db, kb_space, user.user_id):
        # Same shape as a missing asset: an unauthorised caller must not be able to
        # probe which asset ids exist.
        raise ResourceNotFoundError(resource_type="kb_asset", resource_id=asset_id)

    try:
        data = get_storage().download_bytes(asset.storage_key)
    except Exception as exc:
        logger.warning("Asset %s read failed: %s", asset_id, exc)
        raise ResourceNotFoundError(resource_type="kb_asset", resource_id=asset_id)

    return Response(
        content=data,
        media_type=asset.mime_type or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.post("/preview-chunks", summary="预览文档分块效果")
async def preview_chunks(
    file: UploadFile = File(..., description="Document file to preview"),
    chunk_method: str = Form(
        "structured", description="Chunking method: structured|recursive|embedding_semantic|laws|qa"
    ),
    parent_chunk_size: int = Form(1024, description="Parent chunk size in tokens"),
    child_chunk_size: int = Form(128, description="Child chunk size in tokens"),
    overlap_tokens: int = Form(20, description="Overlap between child chunks"),
    parent_child_indexing: bool = Form(
        True, description="Enable parent-child indexing; False = flat chunks only"
    ),
    separators: Optional[str] = Form(
        None, description="父分块分隔符，JSON 数组字符串；为空用默认层级"
    ),
    child_separators: Optional[str] = Form(
        None, description="子分块分隔符，JSON 数组字符串；为空走定长滑窗"
    ),
    user: UserContext = Depends(get_current_user),
):
    """预览文档的分块效果，不落库、不写入向量库。上传文件并按指定分块方法（structured/recursive/embedding_semantic/laws/qa）与父子分块参数解析，返回父块及其子块预览，用于创建知识库前调参。文件大小受 MAX_PREVIEW_SIZE（20MB）限制。"""
    content = await file.read()
    if len(content) > kb_state.MAX_PREVIEW_SIZE:
        raise FileTooLargeError(max_size=kb_state.MAX_PREVIEW_SIZE, actual_size=len(content))
    if len(content) == 0:
        raise BadRequestError(message="File is empty", data={"filename": file.filename})

    if not file.filename:
        raise BadRequestError(message="Filename is required", data={"field": "file"})
    _, mime_type = validate_kb_file(file.filename, content)

    from features.knowledge.parser_tokens import count_tokens as _count_tokens
    from features.knowledge.kb_parser import parse_and_chunk
    from features.knowledge.parser_chunks import parse_separators_form

    _separators = parse_separators_form(separators)
    _child_separators = parse_separators_form(child_separators)

    embed_fn = None
    if chunk_method == "embedding_semantic":
        from core.models.embedding import embed_batch

        embed_fn = embed_batch

    parent_chunks = parse_and_chunk(
        content,
        mime_type,
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
                {
                    "index": c.index,
                    "content": c.content[:200] + ("..." if len(c.content) > 200 else ""),
                }
                for c in pc.children[:5]
            ]
            children_count = len(pc.children)
        else:
            children_preview = []
            children_count = 0
        chunks_preview.append(
            {
                "index": idx,
                "content": pc.content,
                "token_count": _count_tokens(pc.content),
                "children_count": children_count,
                "children_preview": children_preview,
            }
        )

    return success_response(
        data={
            "total_chunks": len(parent_chunks),
            "total_children": total_children,
            "chunks": chunks_preview,
        },
        message="Chunk preview generated successfully",
    )
