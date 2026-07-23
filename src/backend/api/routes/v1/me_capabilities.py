"""Capability center — self-service addition of MCP tools / skills by users.

POST   /v1/me/mcp-servers          add a private remote MCP (HTTP/SSE only)
DELETE /v1/me/mcp-servers/{id}     delete one's own private MCP
POST   /v1/me/skills/upload        upload a private skill zip package
DELETE /v1/me/skills/{skill_id}    delete one's own private skill
GET    /v1/me/skills/{skill_id}/export           export one's own skill as a zip
GET/PUT/DELETE /v1/me/skills/{skill_id}/files/*  read/write/delete files inside the skill folder
POST   /v1/me/skills/{skill_id}/files/upload     upload a single skill file (binary-safe)

Permissions: requires ``can_add_mcp`` / ``can_add_skill`` (controlled by the Config admin platform).
User-created items have ``owner_user_id`` = current user, visible and usable only to them (owner isolation).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional

from core.auth.backend import UserContext, get_current_user
from core.auth.capabilities import resolve_user_capabilities
from core.db.engine import get_db
from core.db.models import AdminMcpServer, AdminSkill
from core.infra.exceptions import AccessDeniedError, BadRequestError, ResourceNotFoundError
from core.infra.responses import created_response, success_response
from core.services.mcp_management_service import probe_mcp_connectivity, refresh_mcp_caches
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

router = APIRouter(prefix="/v1/me", tags=["My Capabilities"])
logger = logging.getLogger(__name__)

# User skill zip cap (smaller than the admin's 200MB, to keep personal uploads from bloating the DB)
USER_SKILL_MAX_BYTES = 50 * 1024 * 1024

# Per-file cap for user skills (online editing / single-file upload), kept an order of magnitude below the zip total cap
USER_SKILL_FILE_MAX_BYTES = 10 * 1024 * 1024


def _require_flag(user_id: str, db: Session, flag: str, label: str) -> None:
    # Personal explicit (user management) → team default (team management) → off by default
    if not resolve_user_capabilities(db, user_id).get(flag, False):
        raise AccessDeniedError(message=f"管理员未开放{label}功能", reason=f"{flag}_disabled")


# ── MCP ──────────────────────────────────────────────────────────────────────


class CreateUserMcpRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=255, description="展示名称")
    description: str = Field("", max_length=2000)
    user_intro: Optional[str] = Field(None, description="能力中心详情页介绍（Markdown）")
    transport: str = Field(
        "streamable_http", pattern=r"^(streamable_http|sse)$", description="仅支持远程 HTTP/SSE"
    )
    url: str = Field(..., description="远程 MCP 端点 URL")
    headers: Dict[str, str] = Field(default_factory=dict, description="自定义请求头（如鉴权）")
    icon: Optional[str] = None


@router.post("/mcp-servers", status_code=201, summary="自助添加私有 MCP")
async def create_my_mcp_server(
    body: CreateUserMcpRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """添加一个仅自己可见可用的远程 MCP 工具（HTTP/SSE）。

    创建即试连：连不上返回 422，避免把不可达端点落库。出于安全，用户入口**不支持
    stdio**（不能在服务器执行任意命令）。
    """
    _require_flag(str(user.user_id), db, "can_add_mcp", "自助添加 MCP")

    if not body.url.strip():
        raise BadRequestError(message="url 不能为空")

    # Auto-generate a globally unique server_id to avoid collisions with public MCPs / other users
    server_id = f"umcp_{uuid.uuid4().hex[:16]}"
    now = datetime.utcnow()
    row = AdminMcpServer(
        server_id=server_id,
        display_name=body.display_name.strip(),
        description=body.description or "",
        user_intro=body.user_intro,
        transport=body.transport,
        url=body.url.strip(),
        headers=body.headers or {},
        is_stable=False,  # user-private MCPs don't enter the warmup connection pool
        is_enabled=True,
        owner_user_id=str(user.user_id),
        icon=body.icon,
        created_at=now,
        updated_at=now,
        created_by=str(user.user_id),
    )

    ok, err = await probe_mcp_connectivity(row, db)
    if not ok:
        raise BadRequestError(message=f"MCP 连接失败，无法添加：{err}")

    db.add(row)
    db.commit()
    db.refresh(row)
    refresh_mcp_caches()

    return created_response(
        data={
            "server_id": row.server_id,
            "display_name": row.display_name,
            "transport": row.transport,
            "url": row.url,
            "owner": "self",
        }
    )


@router.delete("/mcp-servers/{server_id}", summary="删除我的私有 MCP")
async def delete_my_mcp_server(
    server_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除自己添加的私有 MCP。只能删自己的（owner 校验）。"""
    _require_flag(str(user.user_id), db, "can_add_mcp", "自助添加 MCP")
    row = (
        db.query(AdminMcpServer)
        .filter(
            AdminMcpServer.server_id == server_id,
            AdminMcpServer.owner_user_id == str(user.user_id),
        )
        .first()
    )
    if not row:
        raise ResourceNotFoundError("mcp_server", server_id)
    db.delete(row)
    db.commit()

    refresh_mcp_caches()
    return success_response(data={"server_id": server_id, "deleted": True})


# ── Skills ─────────────────────────────────────────────────────────────────


@router.post("/skills/upload", status_code=201, summary="自助上传私有技能")
async def upload_my_skill(
    file: UploadFile = File(...),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """以 zip 包上传一个仅自己可见可用的技能（含 SKILL.md）。

    复用 admin 上传的解析链路，但打上 ``owner_user_id`` 并限 50MB。技能 id 取
    SKILL.md frontmatter ``name``，须全局唯一（与公共/他人技能冲突会被拒绝）。
    """
    _require_flag(str(user.user_id), db, "can_add_skill", "自助添加技能")

    if not file.filename or not file.filename.endswith(".zip"):
        raise BadRequestError(message="仅接受 .zip 技能包")

    data = await file.read()
    if len(data) > USER_SKILL_MAX_BYTES:
        raise BadRequestError(
            message=f"技能包过大（上限 {USER_SKILL_MAX_BYTES // (1024 * 1024)}MB）"
        )

    result = parse_and_upsert_skill_zip(db, data, owner_user_id=str(user.user_id))
    result["owner"] = "self"
    return created_response(data=result)


class CreateUserSkillRequest(BaseModel):
    name: str = Field(
        ..., pattern=r"^[a-z0-9_-]{1,63}$", description="技能 id（小写字母/数字/-/_）"
    )
    display_name: str = Field(..., min_length=1, max_length=255, description="展示名称")
    # description is required and non-empty: the SKILL.md frontmatter description is a hard
    # requirement for skill loading/registration (registry._load_skill_metadata_from_str raises
    # SkillSpecError on an empty value); a skill with an empty description is dropped from the
    # loader metadata → injection is skipped at / invocation → the agent "can't recognize" it.
    description: str = Field(..., min_length=1, max_length=2000, description="一句话描述")
    instructions: str = Field(..., min_length=1, description="技能正文（SKILL.md 指令，Markdown）")
    tags: List[str] = Field(default_factory=list)
    mcp_server_ids: Optional[List[str]] = Field(
        default=None,
        description="绑定的 MCP 服务；省略时编辑操作保留原绑定",
    )
    user_intro: Optional[str] = Field(None, description="能力中心详情页介绍（Markdown）")
    icon: Optional[str] = Field(None, description="图标：preset:<key> / URL / data-URI")

    @field_validator("description", "instructions")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("不能为空")
        return v


@router.post("/skills", status_code=201, summary="自助手写新建技能")
async def create_my_skill(
    body: CreateUserSkillRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """直接手写一个仅自己可见可用的技能（无需打包 zip）。

    技能 id 须全局唯一：不能与公共技能或他人私有技能冲突；与本人已有同名私有技能
    则视为更新。
    """
    _require_flag(str(user.user_id), db, "can_add_skill", "自助添加技能")

    skill_id = body.name
    existing = db.query(AdminSkill).filter(AdminSkill.skill_id == skill_id).first()
    if existing is not None and existing.owner_user_id != str(user.user_id):
        if existing.owner_user_id is None:
            raise HTTPException(
                status_code=409, detail=f"技能 id 「{skill_id}」与公共技能冲突，请改名"
            )
        raise HTTPException(status_code=409, detail=f"技能 id 「{skill_id}」已被占用")

    # Preserve the version of a zip-uploaded skill. MCP-derived tools are rebuilt from the
    # selected servers, while unrelated tool declarations from an imported skill remain intact.
    version = (existing.version if existing else None) or "1.0.0"
    existing_mcp_ids = extract_mcp_server_ids(existing.skill_content if existing else "")
    _, existing_mcp_tools = resolve_mcp_bindings(
        db,
        existing_mcp_ids,
        owner_user_id=str(user.user_id),
        strict=False,
    )
    if body.mcp_server_ids is None:
        mcp_server_ids = existing_mcp_ids
        allowed_tools = list((existing.allowed_tools if existing else None) or [])
    else:
        mcp_server_ids, mcp_tool_names = resolve_mcp_bindings(
            db,
            body.mcp_server_ids,
            owner_user_id=str(user.user_id),
        )
        additional_tools = [
            tool
            for tool in ((existing.allowed_tools if existing else None) or [])
            if tool not in set(existing_mcp_tools)
        ]
        allowed_tools = list(dict.fromkeys([*additional_tools, *mcp_tool_names]))
    ontology_workflows = resolve_ontology_workflows(db, body.tags)
    content = build_skill_content(
        skill_id=skill_id,
        display_name=body.display_name,
        description=body.description,
        version=version,
        tags=body.tags,
        allowed_tools=allowed_tools,
        instructions=body.instructions,
        mcp_server_ids=mcp_server_ids,
        ontology_workflows=ontology_workflows,
    )
    from core.ontology.build_validator import ensure_ontology_build_valid

    ensure_ontology_build_valid(
        db,
        asset_type="skill",
        name=body.display_name or skill_id,
        description=body.description,
        instructions=body.instructions,
        tool_names=list(allowed_tools),
        mcp_server_ids=mcp_server_ids,
        ontology_tags=list(body.tags),
    )
    now = datetime.utcnow()
    if existing is not None:
        existing.skill_content = content
        existing.display_name = body.display_name
        existing.description = body.description
        existing.user_intro = body.user_intro
        existing.tags = body.tags
        existing.allowed_tools = allowed_tools
        existing.is_enabled = True
        existing.updated_at = now
    else:
        db.add(
            AdminSkill(
                skill_id=skill_id,
                skill_content=content,
                display_name=body.display_name,
                description=body.description,
                user_intro=body.user_intro,
                version=version,
                tags=body.tags,
                allowed_tools=allowed_tools,
                is_enabled=True,
                owner_user_id=str(user.user_id),
                created_at=now,
                updated_at=now,
            )
        )
    db.commit()
    if body.icon is not None:
        from core.services.skill_icon_service import set_skill_icon

        set_skill_icon(db, skill_id, body.icon)
    refresh_skill_caches()
    return created_response(
        data={
            "id": skill_id,
            "owner": "self",
            "mcp_server_ids": mcp_server_ids,
            "allowed_tools": allowed_tools,
            "message": "Skill created",
        }
    )


def _get_own_skill(db: Session, user_id: str, skill_id: str) -> AdminSkill:
    """Fetch one's own private skill row with owner verification; 404 if missing or not owned by the user."""
    row = (
        db.query(AdminSkill)
        .filter(
            AdminSkill.skill_id == skill_id,
            AdminSkill.owner_user_id == user_id,
        )
        .first()
    )
    if not row:
        raise ResourceNotFoundError("skill", skill_id)
    return row


@router.get("/skills/{skill_id}", summary="获取我的私有技能（用于编辑）")
async def get_my_skill(
    skill_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """拉取自己私有技能的可编辑字段（含 SKILL.md 正文），供前端编辑表单回填。

    只能读自己的（owner 校验）。正文从 ``skill_content`` 去掉 frontmatter 后回传，
    与手写新建表单的「技能正文」一一对应，编辑保存走 ``POST /v1/me/skills`` upsert。
    附带 ``extra_files`` 清单（文件名/大小/是否二进制），供技能文件管理 UI 渲染。
    """
    _require_flag(str(user.user_id), db, "can_add_skill", "自助添加技能")
    row = _get_own_skill(db, str(user.user_id), skill_id)

    from core.agent_skills.binary_files import is_binary_value
    from core.services.skill_icon_service import get_skill_icon

    extra_files = [
        {
            "filename": fn,
            "size": len(str(content).encode("utf-8")),
            "is_binary": is_binary_value(content),
        }
        for fn, content in sorted((row.extra_files or {}).items())
    ]
    return success_response(
        data={
            "id": row.skill_id,
            "display_name": row.display_name or row.skill_id,
            "description": row.description or "",
            "instructions": extract_instructions(row.skill_content),
            "tags": list(row.tags or []),
            "mcp_server_ids": extract_mcp_server_ids(row.skill_content),
            "allowed_tools": list(row.allowed_tools or []),
            "user_intro": row.user_intro,
            "icon": get_skill_icon(db, row.skill_id),
            "owner": "self",
            "extra_files": extra_files,
        }
    )


# ── Private skill file management (read/write/delete/upload of files inside the skill folder) ──


class UserSkillFileUpdate(BaseModel):
    content: str = Field(..., description="文件内容（UTF-8 文本）")


@router.get("/skills/{skill_id}/files/{filename:path}", summary="读取我的技能文件")
async def get_my_skill_file(
    skill_id: str,
    filename: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """读取自己私有技能中的单个附加文件。二进制文件只返回 ``is_binary=true`` 不回传内容。"""
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
async def save_my_skill_file(
    skill_id: str,
    filename: str,
    body: UserSkillFileUpdate,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """新建或更新自己私有技能中的单个附加文件（UTF-8 文本）。

    SKILL.md 不走本接口（正文在编辑表单里改，经 ``POST /v1/me/skills`` 重建）。
    """
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
    row.updated_at = datetime.utcnow()
    flag_modified(row, "extra_files")
    db.commit()
    refresh_skill_caches()
    return success_response(data={"filename": filename, "message": "File saved"})


@router.delete("/skills/{skill_id}/files/{filename:path}", summary="删除我的技能文件")
async def delete_my_skill_file(
    skill_id: str,
    filename: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除自己私有技能中的单个附加文件。"""
    _require_flag(str(user.user_id), db, "can_add_skill", "自助添加技能")
    row = _get_own_skill(db, str(user.user_id), skill_id)
    extra = dict(row.extra_files or {})
    if filename not in extra:
        raise ResourceNotFoundError("skill_file", filename)
    del extra[filename]
    row.extra_files = extra
    row.updated_at = datetime.utcnow()
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
    row.updated_at = datetime.utcnow()
    flag_modified(row, "extra_files")
    db.commit()
    refresh_skill_caches()
    return success_response(
        data={"filename": filename, "size": len(raw), "message": "File uploaded"}
    )


@router.get("/skills/{skill_id}/export", summary="导出我的技能 zip")
async def export_my_skill(
    skill_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把自己的私有技能完整导出为 zip 包（SKILL.md + 附加文件，二进制还原字节）。

    导出布局与 zip 上传约定一致，可直接重新导入（备份/迁移/分享给管理员上架）。
    """
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


@router.delete("/skills/{skill_id}", summary="删除我的私有技能")
async def delete_my_skill(
    skill_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除自己上传的私有技能。只能删自己的（owner 校验）。"""
    _require_flag(str(user.user_id), db, "can_add_skill", "自助添加技能")
    row = (
        db.query(AdminSkill)
        .filter(
            AdminSkill.skill_id == skill_id,
            AdminSkill.owner_user_id == str(user.user_id),
        )
        .first()
    )
    if not row:
        raise ResourceNotFoundError("skill", skill_id)
    db.delete(row)
    db.commit()

    from core.services.skill_icon_service import delete_skill_icon

    delete_skill_icon(db, skill_id)
    refresh_skill_caches()
    return success_response(data={"skill_id": skill_id, "deleted": True})


class UserSkillIconRequest(BaseModel):
    icon: str = Field("", description="图标：preset:<key> / URL / data-URI；空串=恢复默认")


@router.put("/skills/{skill_id}/icon", summary="设置我的私有技能图标")
async def set_my_skill_icon(
    skill_id: str,
    body: UserSkillIconRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """给自己的私有技能设置图标（owner 校验）。空串=恢复默认。"""
    _require_flag(str(user.user_id), db, "can_add_skill", "自助添加技能")
    row = (
        db.query(AdminSkill)
        .filter(AdminSkill.skill_id == skill_id, AdminSkill.owner_user_id == str(user.user_id))
        .first()
    )
    if not row:
        raise ResourceNotFoundError("skill", skill_id)

    from core.services.skill_icon_service import set_skill_icon

    icon = set_skill_icon(db, skill_id, body.icon)
    refresh_skill_caches()
    return success_response(data={"id": skill_id, "icon": icon})
