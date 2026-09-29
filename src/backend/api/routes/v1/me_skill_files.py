from __future__ import annotations
from core.infra.time import utc_now

import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from core.capabilities.paths import capabilities_enabled
from core.services import local_skill_editor as local_editor
from core.auth.backend import UserContext, get_current_user
from core.auth.capabilities import resolve_user_capabilities
from core.config.settings import settings
from core.db.engine import get_db
from core.db.models import AdminMcpServer, AdminSkill
from core.infra.exceptions import AccessDeniedError, BadRequestError, ResourceNotFoundError
from core.infra.responses import created_response, success_response
from core.services.mcp_management_service import (
    encrypt_mcp_headers,
    probe_mcp_connectivity,
    refresh_mcp_caches,
    validate_remote_mcp_url,
)
from core.services.skill_management_service import (
    build_skill_content,
    extract_instructions,
    extract_mcp_server_ids,
    parse_and_upsert_skill_zip,
    refresh_skill_caches,
    resolve_mcp_bindings,
    resolve_ontology_workflows,
    validate_skill_file_path,
)
from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

router = APIRouter()
logger = logging.getLogger(__name__)

from .me_capabilities import _require_flag, _get_own_skill, USER_SKILL_FILE_MAX_BYTES

# ── Private skill file management (read/write/delete/upload of files inside the skill folder) ──


class UserSkillFileUpdate(BaseModel):
    content: str = Field(..., description="文件内容（UTF-8 文本）")


@router.get("/skills/{skill_id}/files/{filename:path}", summary="读取我的技能文件")
def get_my_skill_file(
    skill_id: str,
    filename: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """读取自己私有技能中的单个附加文件。二进制文件只返回 ``is_binary=true`` 不回传内容。"""
    if capabilities_enabled():
        return success_response(data=local_editor.file_get(str(user.user_id), skill_id, filename))
    _require_flag(str(user.user_id), db, "can_add_skill", "自助添加技能")
    row = _get_own_skill(db, str(user.user_id), skill_id)

    from core.agent_skills.binary_files import is_binary_value

    extra = row.extra_files or {}
    if filename not in extra:
        raise ResourceNotFoundError("skill_file", filename)
    stored = extra[filename]
    if is_binary_value(stored):
        return success_response(data={"filename": filename, "content": "", "is_binary": True})
    return success_response(data={"filename": filename, "content": stored, "is_binary": False})


@router.put("/skills/{skill_id}/files/{filename:path}", summary="保存我的技能文件")
def save_my_skill_file(
    skill_id: str,
    filename: str,
    body: UserSkillFileUpdate,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """新建或更新自己私有技能中的单个附加文件（UTF-8 文本）。

    SKILL.md 不走本接口（正文在编辑表单里改，经 ``POST /v1/me/skills`` 重建）。
    """
    if capabilities_enabled():
        return success_response(data=local_editor.file_change(str(user.user_id), skill_id, filename, body.content.encode("utf-8")))
    _require_flag(str(user.user_id), db, "can_add_skill", "自助添加技能")

    filename = validate_skill_file_path(filename)
    if filename == "SKILL.md":
        raise BadRequestError(message="SKILL.md 请在「编辑技能」表单中修改")
    if len(body.content.encode("utf-8")) > USER_SKILL_FILE_MAX_BYTES:
        raise BadRequestError(
            message=f"文件过大（上限 {USER_SKILL_FILE_MAX_BYTES // (1024 * 1024)}MB）"
        )
    row = _get_own_skill(db, str(user.user_id), skill_id)
    extra = dict(row.extra_files or {})
    extra[filename] = body.content
    row.extra_files = extra
    row.updated_at = utc_now()
    flag_modified(row, "extra_files")
    db.commit()
    refresh_skill_caches()
    return success_response(data={"filename": filename, "message": "File saved"})


@router.delete("/skills/{skill_id}/files/{filename:path}", summary="删除我的技能文件")
def delete_my_skill_file(
    skill_id: str,
    filename: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除自己私有技能中的单个附加文件。"""
    if capabilities_enabled():
        return success_response(data=local_editor.file_change(str(user.user_id), skill_id, filename))
    _require_flag(str(user.user_id), db, "can_add_skill", "自助添加技能")
    row = _get_own_skill(db, str(user.user_id), skill_id)
    extra = dict(row.extra_files or {})
    if filename not in extra:
        raise ResourceNotFoundError("skill_file", filename)
    del extra[filename]
    row.extra_files = extra
    row.updated_at = utc_now()
    flag_modified(row, "extra_files")
    db.commit()

    refresh_skill_caches()
    return success_response(data={"filename": filename, "message": "File deleted"})


@router.post("/skills/{skill_id}/files/upload", status_code=201, summary="上传我的技能文件")
async def upload_my_skill_file(
    skill_id: str,
    file: UploadFile = File(...),
    path: str = Form("", description="可选：存入的相对路径（含子目录），留空取上传文件名"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """以 multipart 上传单个文件到自己的私有技能，二进制按 base64 标记安全存储。"""
    if capabilities_enabled():
        return success_response(data=local_editor.file_change(str(user.user_id), skill_id, path or file.filename or "", await file.read()))
    _require_flag(str(user.user_id), db, "can_add_skill", "自助添加技能")

    from core.agent_skills.binary_files import encode_upload

    filename = validate_skill_file_path(path or file.filename or "")
    if filename == "SKILL.md":
        raise BadRequestError(message="SKILL.md 请在「编辑技能」表单中修改")
    raw = await file.read()
    if len(raw) > USER_SKILL_FILE_MAX_BYTES:
        raise BadRequestError(
            message=f"文件过大（上限 {USER_SKILL_FILE_MAX_BYTES // (1024 * 1024)}MB）"
        )
    row = _get_own_skill(db, str(user.user_id), skill_id)
    extra = dict(row.extra_files or {})
    extra[filename] = encode_upload(filename, raw)
    row.extra_files = extra
    row.updated_at = utc_now()
    flag_modified(row, "extra_files")
    db.commit()
    refresh_skill_caches()
    return success_response(
        data={"filename": filename, "size": len(raw), "message": "File uploaded"}
    )


@router.get("/skills/{skill_id}/export", summary="导出我的技能 zip")
def export_my_skill(
    skill_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把自己的私有技能完整导出为 zip 包（SKILL.md + 附加文件，二进制还原字节）。

    导出布局与 zip 上传约定一致，可直接重新导入（备份/迁移/分享给管理员上架）。
    """
    if capabilities_enabled():
        return Response(content=local_editor.export(str(user.user_id), skill_id), media_type="application/zip")
    _require_flag(str(user.user_id), db, "can_add_skill", "自助添加技能")
    row = _get_own_skill(db, str(user.user_id), skill_id)

    from core.services.marketplace_service import build_skill_zip

    data = build_skill_zip(skill_id, row.skill_content or "", row.extra_files or {})
    safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in skill_id) or "skill"
    logger.info("user_skill_exported_zip: %s by %s (%d bytes)", skill_id, user.user_id, len(data))
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'},
    )
