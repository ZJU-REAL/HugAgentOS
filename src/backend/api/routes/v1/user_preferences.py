"""Authenticated user preferences stored in user metadata."""
from typing import Literal, List, Optional
from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from core.auth.backend import UserContext, get_current_user
from core.config.settings import DEFAULT_CHAT_MODEL_ALIAS
from core.db.engine import get_db
from core.db.models import UserShadow
from core.db.repository import CatalogRepository, UserRepository
from core.infra.exceptions import AccessDeniedError, ResourceNotFoundError
from core.infra.responses import success_response
router = APIRouter(prefix="/v1", tags=["Users"])

class UserPreferences(BaseModel):
    """User preferences model."""

    sidebar_layout: Optional[Literal["classic", "modern"]] = None
    default_model: Optional[str] = Field(None, description="Default AI model")
    language: Optional[str] = Field(None, description="Preferred language")
    theme: Optional[str] = Field(None, description="UI theme: light, dark, auto")
    enabled_skills: Optional[List[str]] = Field(
        default=None, description="Enabled skill IDs"
    )
    enabled_mcps: Optional[List[str]] = Field(
        default=None, description="Enabled MCP server IDs"
    )


@router.get("/users/{user_id}/preferences", summary="获取用户偏好设置")
def get_user_preferences(
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
        "sidebar_layout": "classic" if preferences.get("sidebar_layout") == "classic" else "modern",
        "enabled_skills": enabled_skills,
        "enabled_mcps": enabled_mcps,
    }

    return success_response(data=data, message="User preferences retrieved successfully")


@router.put("/users/{user_id}/preferences", summary="更新用户偏好设置")
def update_user_preferences(
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

    # Lock the user row before merging JSON preferences to preserve concurrent updates.
    user_shadow = db.query(UserShadow).filter(UserShadow.user_id == user_id).with_for_update().populate_existing().one()

    # Update metadata preferences
    metadata = dict(user_shadow.extra_data or {})
    metadata["preferences"] = dict(metadata.get("preferences") or {})

    # Update preference fields
    if preferences.default_model is not None:
        metadata["preferences"]["default_model"] = preferences.default_model
    if preferences.language is not None:
        metadata["preferences"]["language"] = preferences.language
    if preferences.theme is not None:
        metadata["preferences"]["theme"] = preferences.theme

    if preferences.sidebar_layout is not None:
        metadata["preferences"]["sidebar_layout"] = preferences.sidebar_layout

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
