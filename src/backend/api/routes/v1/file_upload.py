"""File upload and update endpoints."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.models import Artifact, ChatSession, UserFolder, UserShadow
from core.files.artifacts.service import store_bytes_as_artifact
from core.files.storage import get_storage
from core.files.upload_retry import prepare_upload
from .file_text import router as text_router

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/file", tags=["file"])

_MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


@router.post("/upload", summary="上传用户文件到 OSS 持久存储")
def upload_user_file(
    file: UploadFile = File(...),
    chat_id: Optional[str] = Form(None),
    upload_key: Optional[str] = Form(None, max_length=36),
    folder_id: Optional[str] = Form(None, description="可选个人文件夹 ID（我的空间）；省略=根目录"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    将用户上传的文件持久化到 OSS，写入 Artifact 表，返回文件 ID 和下载路径。

    **返回**：
    - `file_id`: 文件唯一 ID
    - `name`: 文件名
    - `size`: 文件字节数
    - `mime_type`: MIME 类型
    - `download_url`: 下载路径（`/files/{file_id}`）
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")

    from core.db.personal_file_names import validate_filename
    validate_filename(file.filename)
    file_bytes = file.file.read(_MAX_UPLOAD_BYTES + 1)
    if not file_bytes:
        raise HTTPException(status_code=400, detail="文件内容为空")

    if len(file_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件过大，最大支持 50 MB")

    user_id = str(user.user_id)

    public_meta = {}
    if user.public_chat_visitor:
        from core.chat.public import prepare_visitor_upload
        public_meta = prepare_visitor_upload(db, user, chat_id, folder_id)

    # Verify the chat_id already exists in the database (the session may not yet be created at upload time)
    db_chat_id: Optional[str] = None
    if chat_id:
        exists = db.query(ChatSession.chat_id).filter(ChatSession.chat_id == chat_id).first()
        if exists:
            db_chat_id = chat_id

    artifact_id, retry_meta, existing = prepare_upload(
        db,
        key=upload_key,
        user_id=user_id,
        scope="personal",
        folder_id=folder_id,
        filename=file.filename,
        content=file_bytes,
        chat_id=db_chat_id,
    )

    # Serialize this user's uploads (before any folder lock) so the free-name pick below cannot race.
    from core.db.repository.folder import lock_folder_owner
    lock_folder_owner(db, UserShadow, "user_id", user_id)

    # Verify folder_id must be a valid personal folder belonging to the current user
    db_folder_id: Optional[str] = None
    if folder_id:
        folder = (
            db.query(UserFolder)
            .filter(
                UserFolder.folder_id == folder_id,
                UserFolder.user_id == user_id,
                UserFolder.deleted_at.is_(None),
            )
            .populate_existing()
            .with_for_update()
            .first()
        )
        if not folder:
            raise HTTPException(status_code=400, detail="目标文件夹不存在")
        db_folder_id = folder_id

    # Every upload, chat attachments included, also lands in My Space (root when no folder).
    # Callers without a personal space (API keys, public-chat visitors) keep the artifact only.
    from core.files.spaces.files import SpaceFiles
    from core.files.spaces.projects import folder_path
    space: Optional[SpaceFiles] = SpaceFiles(db, user_id, get_storage())
    try:
        space.grant("personal", write=True)
    except HTTPException:
        space = None
    if user.public_chat_visitor and not db_folder_id:
        space = None
    directory = folder_path(db, UserFolder, {"user_id": user_id}, db_folder_id) if space else ""

    def space_path(name: str) -> str:
        return f"{directory}/{name}" if directory else name

    # A taken name is not an error: keep both files, the new one as "name (1).ext".
    from core.db.personal_file_names import available_name
    filename = existing.filename if existing else available_name(
        db, user_id, db_folder_id, file.filename,
        taken=(lambda name: space.occupied("personal", space_path(name))) if space else None,
    )
    if retry_meta:
        retry_meta["upload_name"] = filename

    # Leave summary / parsed_text empty. The first model turn that references
    # this file parses it server-side and caches both fields for later reads.
    try:
        artifact = existing or store_bytes_as_artifact(
            db,
            user_id=user_id,
            content=file_bytes,
            filename=filename,
            mime_type=file.content_type,
            chat_id=db_chat_id,
            user_folder_id=db_folder_id,
            source="user_upload",
            artifact_id=artifact_id,
            extra={**retry_meta, **public_meta},
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"文件上传失败: {e}")

    if space:
        # Two copies on purpose: the artifact is the chat's immutable snapshot, the space file is
        # the editable working copy. Copy server-side instead of uploading the bytes a second time.
        space.copy_from("personal", space_path(filename), artifact.storage_key, artifact.size_bytes)

    return {
        "file_id": artifact.artifact_id,
        "name": artifact.filename,
        "size": artifact.size_bytes,
        "mime_type": artifact.mime_type,
        "download_url": f"/files/{artifact.artifact_id}",
    }


@router.put("/{file_id}", summary="覆盖已有文件内容")
def overwrite_file(
    file_id: str,
    file: UploadFile = File(...),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    用新内容覆盖已有文件，保持 file_id 和 download_url 不变。
    """
    file_bytes = file.file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="文件内容为空")
    if len(file_bytes) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件过大，最大支持 50 MB")

    from core.files.artifacts.local_reference import is_local_file_ref, overwrite_reference
    if is_local_file_ref(file_id):
        return overwrite_reference(file_id, str(user.user_id), file_bytes, db)

    from core.auth.edition import resolve_artifact_access
    artifact = db.query(Artifact).filter(
        Artifact.artifact_id == file_id, Artifact.deleted_at.is_(None),
    ).populate_existing().with_for_update().first()
    from core.files.edition import artifact_access_metadata
    metadata = artifact_access_metadata(artifact) if artifact else {}
    level = resolve_artifact_access(db, str(user.user_id), metadata.get("owner_id"), metadata.get("scope_id")) if artifact else "none"
    if level == "none":
        raise HTTPException(404, "文件不存在")
    if level not in ("edit", "admin"):
        raise HTTPException(403, "当前文件只读")
    storage = get_storage()

    # Preserve last-good bytes until the replacement transaction is committed.
    import uuid
    replacement_key = f"file_versions/{file_id}/{uuid.uuid4().hex}"
    try:
        storage_url = storage.upload_bytes(file_bytes, replacement_key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文件上传失败: {e}")
    artifact.storage_key = replacement_key
    artifact.parsed_text = None
    artifact.summary = None

    artifact.size_bytes = len(file_bytes)
    artifact.storage_url = storage_url
    artifact.mime_type = file.content_type or artifact.mime_type
    artifact.updated_at = datetime.now(timezone.utc)
    db.commit()

    return {
        "file_id": file_id,
        "name": artifact.filename,
        "size": len(file_bytes),
        "mime_type": artifact.mime_type,
        "download_url": f"/files/{file_id}",
    }


# Registered through the existing CE/EE file router entry.
router.include_router(text_router)
