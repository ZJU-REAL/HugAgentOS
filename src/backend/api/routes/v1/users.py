"""User management API routes (v1)."""

import logging
import os
import re
import time
import uuid
from typing import List, Optional

from core.auth.backend import UserContext, get_current_user
from core.config.settings import DEFAULT_CHAT_MODEL_ALIAS
from core.db.engine import get_db
from core.db.repository import CatalogRepository, UserRepository
from core.infra.exceptions import AccessDeniedError, ResourceNotFoundError, ValidationError
from core.infra.responses import success_response
from core.storage import get_storage
from fastapi import APIRouter, Depends, File, HTTPException, Path, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Users"])

# ── Avatar upload-related constants ──────────────────────────────────────
_AVATAR_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
_AVATAR_MIME_TO_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
# Allowlist matching rules for setting a "known URL" (default avatar / default fallback)
_AVATAR_URL_DEFAULT_PATTERNS = (
    re.compile(r"^/icons/avatar/avatar-[1-8]\.png$"),
    re.compile(r"^/home/default-avatar\.svg$"),
)
# Past/future raw endpoint path, used to recognize an "uploaded avatar" when writing back
_AVATAR_RAW_PREFIX = "/v1/users/"
_AVATAR_RAW_SUFFIX = "/avatar/raw"


def _build_avatar_raw_url(user_id: str) -> str:
    """Build the access URL for an uploaded avatar, with a timestamp for browser cache busting."""
    return f"{_AVATAR_RAW_PREFIX}{user_id}{_AVATAR_RAW_SUFFIX}?v={int(time.time())}"


def _delete_old_avatar_storage(meta: dict) -> None:
    """Delete the old avatar storage file recorded in metadata; on failure only log, never raise."""
    old_key = meta.get("avatar_storage_key")
    if not old_key:
        return
    try:
        get_storage().delete(old_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("avatar_old_delete_failed", storage_key=old_key, error=str(exc))


# Request/Response Models
class UserPreferences(BaseModel):
    """User preferences model."""

    default_model: Optional[str] = Field(DEFAULT_CHAT_MODEL_ALIAS, description="Default AI model")
    language: Optional[str] = Field("zh-CN", description="Preferred language")
    theme: Optional[str] = Field("auto", description="UI theme: light, dark, auto")
    enabled_skills: Optional[List[str]] = Field(
        default_factory=list, description="Enabled skill IDs"
    )
    enabled_mcps: Optional[List[str]] = Field(
        default_factory=list, description="Enabled MCP server IDs"
    )


@router.get("/me", summary="获取当前用户信息（含部门、团队、本地账号资料）")
async def get_current_user_info(
    user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)
):
    """获取当前登录用户信息，包含部门、所属团队及本地账号资料（昵称/真实姓名/电话）。"""
    user_repo = UserRepository(db)
    user_shadow = user_repo.get_by_id(user.user_id)

    if not user_shadow:
        raise ResourceNotFoundError(resource_type="user", resource_id=user.user_id)

    from core.db.models import LocalUser

    try:
        from core.services.team_service import list_user_teams_brief
    except (
        ModuleNotFoundError
    ):  # CE: single-tenant has no teams -- only degrade the teams field, user info returns as usual
        list_user_teams_brief = None

    local = db.query(LocalUser).filter(LocalUser.user_id == user.user_id).first()
    teams = list_user_teams_brief(db, user.user_id) if list_user_teams_brief else []
    meta = dict(user_shadow.extra_data or {})

    data = {
        "user_id": user_shadow.user_id,
        "user_center_id": user_shadow.user_center_id,
        "username": user_shadow.username,
        "email": user_shadow.email,
        "avatar": user_shadow.avatar_url,
        "avatar_url": user_shadow.avatar_url,
        "nickname": local.nickname if local else None,
        "real_name": local.real_name if local else None,
        "phone": local.phone if local else None,
        "department": meta.get("department"),
        "auth_source": "local" if local else "external",
        "must_change_password": bool(meta.get("must_change_password")),
        "teams": teams,
        "created_at": user_shadow.created_at.isoformat(),
    }

    return success_response(data=data, message="User information retrieved successfully")


class UpdateMyProfileRequest(BaseModel):
    """Profile fields the current user can self-update (all optional, updated only when provided)."""

    nickname: Optional[str] = Field(None, description="用户名（昵称），1-32 位")
    real_name: Optional[str] = Field(None, description="真实姓名")
    phone: Optional[str] = Field(None, description="联系电话")


class ChangeMyPasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)


@router.patch("/me", summary="更新当前用户资料（本地账号可改 nickname / real_name / phone）")
async def update_current_user_info(
    payload: UpdateMyProfileRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新当前用户资料（nickname / real_name / phone，仅传的字段才更新）。仅本地账号可改，外部 SSO 用户需到身份源修改。"""
    from core.db.models import LocalUser
    from core.infra.exceptions import ValidationError
    from core.services.local_user_service import LocalUserService

    local = db.query(LocalUser).filter(LocalUser.user_id == user.user_id).first()
    if not local:
        raise AccessDeniedError(
            message="仅本地账号可在此修改资料",
            reason="external SSO users must update profile in their identity provider",
        )

    service = LocalUserService(db)
    result = service.update_profile(
        user_id=user.user_id,
        nickname=payload.nickname,
        real_name=payload.real_name,
        phone=payload.phone,
    )
    if not result.ok:
        raise ValidationError(
            errors=[{"message": result.message or "更新失败"}], message=result.message or "更新失败"
        )

    return success_response(data=result.user_info, message=result.message)


@router.put("/me/password", summary="修改当前本地账号密码")
async def change_my_password(
    payload: ChangeMyPasswordRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Verify the current password, save the replacement, and clear the first-login flag."""
    from core.services.local_user_service import LocalUserService

    result = LocalUserService(db).change_password(
        user_id=user.user_id,
        old_password=payload.old_password,
        new_password=payload.new_password,
    )
    if not result.ok:
        raise ValidationError(
            errors=[{"field": "password", "message": result.message}],
            message=result.message,
        )
    return success_response(
        data={"user_id": user.user_id, "must_change_password": False},
        message=result.message,
    )


# ── Avatar management ────────────────────────────────────────────────────────
# Three write operations + one read:
#   POST   /v1/me/avatar           upload an image, store it to storage
#   PUT    /v1/me/avatar           set to a known URL (default avatar)
#   DELETE /v1/me/avatar           clear the custom avatar, back to default
#   GET    /v1/users/{uid}/avatar/raw  read the uploaded avatar byte stream


class SetAvatarUrlRequest(BaseModel):
    """Set the avatar via URL (used for choosing a built-in default avatar)."""

    avatar_url: Optional[str] = Field(None, description="头像 URL；传 null 等价于 DELETE")


def _persist_avatar_url(
    db: Session,
    user_repo: UserRepository,
    user_shadow,
    new_avatar_url: Optional[str],
    new_storage_key: Optional[str],
) -> dict:
    """Uniformly update avatar_url + extra_data['avatar_storage_key'] + clean up the old file."""
    meta = dict(user_shadow.extra_data or {})
    _delete_old_avatar_storage(meta)
    if new_storage_key:
        meta["avatar_storage_key"] = new_storage_key
    else:
        meta.pop("avatar_storage_key", None)
    user_repo.update(
        user_shadow.user_id,
        {
            "avatar_url": new_avatar_url,
            "extra_data": meta,
        },
    )
    return {
        "user_id": user_shadow.user_id,
        "avatar_url": new_avatar_url,
    }


@router.post("/me/avatar", summary="上传自定义头像（multipart）")
async def upload_my_avatar(
    file: UploadFile = File(..., description="头像图片，≤2MB，支持 png/jpg/webp/gif"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """上传自定义头像图片（≤2MB，支持 png/jpg/webp/gif），写入存储并更新用户头像 URL，自动清理旧文件。"""
    content_type = (file.content_type or "").lower()
    ext = _AVATAR_MIME_TO_EXT.get(content_type)
    if not ext:
        raise ValidationError(
            errors=[{"field": "file", "message": f"不支持的图片类型：{content_type or '未知'}"}],
            message="头像仅支持 PNG / JPG / WEBP / GIF",
        )

    file_bytes = await file.read()
    if not file_bytes:
        raise ValidationError(
            errors=[{"field": "file", "message": "文件内容为空"}],
            message="头像文件为空",
        )
    if len(file_bytes) > _AVATAR_MAX_BYTES:
        raise ValidationError(
            errors=[
                {"field": "file", "message": f"超过 {_AVATAR_MAX_BYTES // 1024 // 1024}MB 上限"}
            ],
            message="头像文件过大",
        )

    user_repo = UserRepository(db)
    user_shadow = user_repo.get_by_id(user.user_id)
    if not user_shadow:
        raise ResourceNotFoundError(resource_type="user", resource_id=user.user_id)

    env = os.getenv("ENVIRONMENT", "dev")
    storage_key = f"{env}/avatars/{user.user_id}/{uuid.uuid4().hex}.{ext}"
    try:
        get_storage().upload_bytes(file_bytes, storage_key)
    except Exception as exc:  # noqa: BLE001
        logger.error("avatar_upload_failed", user_id=user.user_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"头像上传失败：{exc}")

    new_url = _build_avatar_raw_url(user.user_id)
    data = _persist_avatar_url(db, user_repo, user_shadow, new_url, storage_key)
    return success_response(data=data, message="头像已更新")


@router.put("/me/avatar", summary="将头像设置成已知 URL（如内置默认头像）")
async def set_my_avatar_url(
    payload: SetAvatarUrlRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """将头像设置为已知 URL（仅允许内置默认头像白名单），传 null 等价于清除头像。"""
    new_url = (payload.avatar_url or "").strip() or None
    if new_url is not None:
        if len(new_url) > 1024:
            raise ValidationError(
                errors=[{"field": "avatar_url", "message": "URL 过长"}],
                message="头像 URL 过长",
            )
        if not any(p.match(new_url) for p in _AVATAR_URL_DEFAULT_PATTERNS):
            raise ValidationError(
                errors=[{"field": "avatar_url", "message": "仅允许设置成内置默认头像"}],
                message="不支持的头像 URL；如需自定义头像请使用上传接口",
            )

    user_repo = UserRepository(db)
    user_shadow = user_repo.get_by_id(user.user_id)
    if not user_shadow:
        raise ResourceNotFoundError(resource_type="user", resource_id=user.user_id)

    data = _persist_avatar_url(db, user_repo, user_shadow, new_url, None)
    return success_response(data=data, message="头像已更新")


@router.delete("/me/avatar", summary="清除头像，回到默认")
async def clear_my_avatar(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """清除当前用户的自定义头像，回到默认头像，并删除旧的头像存储文件。"""
    user_repo = UserRepository(db)
    user_shadow = user_repo.get_by_id(user.user_id)
    if not user_shadow:
        raise ResourceNotFoundError(resource_type="user", resource_id=user.user_id)
    data = _persist_avatar_url(db, user_repo, user_shadow, None, None)
    return success_response(data=data, message="头像已清除")


@router.get(
    "/users/{user_id}/avatar/raw",
    summary="读取用户上传头像的原始字节",
    response_class=Response,
)
async def get_user_avatar_raw(
    user_id: str = Path(..., description="目标用户 ID"),
    _user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """同源 cookie 鉴权后任意登录用户都可读取（用于团队成员之间互相看到头像）。"""
    user_repo = UserRepository(db)
    target = user_repo.get_by_id(user_id)
    if not target:
        raise ResourceNotFoundError(resource_type="user", resource_id=user_id)

    storage_key = (target.extra_data or {}).get("avatar_storage_key")
    if not storage_key:
        raise HTTPException(status_code=404, detail="该用户未上传自定义头像")

    try:
        data = get_storage().download_bytes(storage_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("avatar_read_failed", user_id=user_id, error=str(exc))
        raise HTTPException(status_code=404, detail="头像文件不存在")

    ext = storage_key.rsplit(".", 1)[-1].lower()
    mime = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "gif": "image/gif",
    }.get(ext, "application/octet-stream")
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/users/{user_id}/preferences", summary="获取用户偏好设置")
async def get_user_preferences(
    user_id: str = Path(..., description="User ID"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """获取用户偏好设置（默认模型、语言、主题，及启用的技能/MCP 列表）。仅能访问本人偏好。"""
    # Check if user is accessing their own preferences
    if user_id != user.user_id:
        raise AccessDeniedError(
            message="Access denied", reason="Users can only access their own preferences"
        )

    user_repo = UserRepository(db)
    catalog_repo = CatalogRepository(db)

    # Get user shadow
    user_shadow = user_repo.get_by_id(user_id)
    if not user_shadow:
        raise ResourceNotFoundError(resource_type="user", resource_id=user_id)

    # Get metadata preferences
    metadata = user_shadow.extra_data or {}
    preferences = metadata.get("preferences", {})

    # Get enabled skills and MCPs from catalog overrides
    overrides = catalog_repo.list_overrides(user_id)
    enabled_skills = [o.item_id for o in overrides if o.kind == "skill" and o.enabled]
    enabled_mcps = [o.item_id for o in overrides if o.kind == "mcp" and o.enabled]

    # Build preferences response
    data = {
        "default_model": preferences.get("default_model", DEFAULT_CHAT_MODEL_ALIAS),
        "language": preferences.get("language", "zh-CN"),
        "theme": preferences.get("theme", "auto"),
        "enabled_skills": enabled_skills,
        "enabled_mcps": enabled_mcps,
    }

    return success_response(data=data, message="User preferences retrieved successfully")


@router.put("/users/{user_id}/preferences", summary="更新用户偏好设置")
async def update_user_preferences(
    preferences: UserPreferences,
    user_id: str = Path(..., description="User ID"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新用户偏好设置，字段均可选、仅传的才更新。仅能更新本人偏好；enabled_skills/enabled_mcps 同步到 catalog_overrides 表。"""
    # Check if user is updating their own preferences
    if user_id != user.user_id:
        raise AccessDeniedError(
            message="Access denied", reason="Users can only update their own preferences"
        )

    user_repo = UserRepository(db)
    catalog_repo = CatalogRepository(db)

    # Get user shadow
    user_shadow = user_repo.get_by_id(user_id)
    if not user_shadow:
        raise ResourceNotFoundError(resource_type="user", resource_id=user_id)

    # Update metadata preferences
    metadata = user_shadow.extra_data or {}
    if "preferences" not in metadata:
        metadata["preferences"] = {}

    # Update preference fields
    if preferences.default_model is not None:
        metadata["preferences"]["default_model"] = preferences.default_model
    if preferences.language is not None:
        metadata["preferences"]["language"] = preferences.language
    if preferences.theme is not None:
        metadata["preferences"]["theme"] = preferences.theme

    # Save metadata
    user_repo.update(user_id, {"extra_data": metadata})

    # Update catalog overrides for skills and MCPs
    if preferences.enabled_skills is not None:
        # Get all existing skill overrides
        existing_skills = catalog_repo.list_overrides(user_id, kind="skill")

        # Update enabled status for all skills
        for skill_id in preferences.enabled_skills:
            catalog_repo.upsert_override(
                user_id=user_id, kind="skill", item_id=skill_id, enabled=True
            )

        # Disable skills that are not in the enabled list
        for skill in existing_skills:
            if skill.item_id not in preferences.enabled_skills:
                catalog_repo.upsert_override(
                    user_id=user_id, kind="skill", item_id=skill.item_id, enabled=False
                )

    if preferences.enabled_mcps is not None:
        # Get all existing MCP overrides
        existing_mcps = catalog_repo.list_overrides(user_id, kind="mcp")

        # Update enabled status for all MCPs
        for mcp_id in preferences.enabled_mcps:
            catalog_repo.upsert_override(user_id=user_id, kind="mcp", item_id=mcp_id, enabled=True)

        # Disable MCPs that are not in the enabled list
        for mcp in existing_mcps:
            if mcp.item_id not in preferences.enabled_mcps:
                catalog_repo.upsert_override(
                    user_id=user_id, kind="mcp", item_id=mcp.item_id, enabled=False
                )

    return success_response(message="User preferences updated successfully")
