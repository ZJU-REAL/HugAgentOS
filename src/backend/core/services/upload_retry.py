"""Stable retry identities for MySpace uploads; authorization stays in routes."""

import hashlib
import json
from uuid import UUID

from core.db.models import Artifact, UserShadow
from core.services.artifact_edition import upload_target_matches
from core.services.folder_batch import lock_folder_owner
from fastapi import HTTPException


def prepare_upload(db, *, key, user_id, scope, folder_id, filename, content, chat_id=None):
    if key is None:
        return None, {}, None
    try:
        UUID(key)
    except (ValueError, AttributeError):
        raise HTTPException(400, "上传重试标识非法")
    # Serialize keyed writes for one user before taking any folder lock.
    lock_folder_owner(db, UserShadow, "user_id", user_id)
    identity = hashlib.sha256(json.dumps([user_id, key]).encode()).hexdigest()[:48]
    fingerprint = hashlib.sha256(
        json.dumps([scope, folder_id, filename, chat_id], ensure_ascii=False).encode()
        + b"\x00"
        + content
    ).hexdigest()
    artifact_id = "up_" + identity
    existing = db.query(Artifact).filter(Artifact.artifact_id == artifact_id).first()
    moved = existing is not None and (
        not upload_target_matches(existing, scope, folder_id)
        or existing.filename != filename
        or existing.storage_key != (existing.extra_data or {}).get("upload_storage_key")
    )
    if existing and (
        moved
        or existing.deleted_at is not None
        or (existing.extra_data or {}).get("upload_fingerprint") != fingerprint
    ):
        raise HTTPException(409, "上传内容或目标已改变，请创建新的上传任务")
    return artifact_id, {"upload_fingerprint": fingerprint}, existing
