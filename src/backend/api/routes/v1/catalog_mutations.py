"""Capability catalog preference mutations."""
from typing import Any, Dict, Optional
from core.auth.backend import UserContext, get_current_user
from core.config.catalog_runtime import get_runtime_catalog
from core.db.engine import get_db
from core.infra.exceptions import BadRequestError
from core.infra.responses import success_response
from core.services import CatalogService
from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from .catalog_ownership import _is_owned_capability
router = APIRouter()

# Request/Response Models
class UpdateCatalogRequest(BaseModel):
    """Request model for updating catalog configuration."""

    enabled: Optional[bool] = Field(None, description="Enable/disable the item")
    config: Optional[Dict[str, Any]] = Field(None, description="Configuration overrides")


class CatalogItemResponse(BaseModel):
    """Response model for a catalog item."""

    id: str
    name: str
    description: str
    enabled: bool
    config: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None


@router.patch("/{kind}/{id}", summary="更新能力配置")
def update_catalog_item(
    kind: str = Path(..., description="Item kind: skill, agent, mcp, or kb"),
    id: str = Path(..., description="Item ID"),
    request: UpdateCatalogRequest = ...,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """更新当前用户对某能力项的配置（启用状态 enabled 与配置覆盖 config）。

    覆盖按用户独立存储，不影响其他用户或系统级默认值。kind 取值：skill / agent /
    mcp / kb（kb 仅运行时开关，不落库）。管理员在目录层禁用的项不可被用户重新启用；
    技能或工具变更会失效系统提示词缓存。
    """
    # Normalize kind
    kind_map = {
        "skill": "skill",
        "skills": "skill",
        "agent": "agent",
        "agents": "agent",
        "mcp": "mcp",
        "mcp_server": "mcp",
        "mcp_servers": "mcp",
        "kb": "kb",
        "knowledge_base": "kb",
        "knowledge_bases": "kb",
    }

    normalized_kind = kind_map.get(kind.lower())
    if not normalized_kind:
        raise BadRequestError(
            message="Invalid kind",
            data={"allowed_kinds": ["skill", "agent", "mcp", "kb"], "provided_kind": kind},
        )

    # Validate that at least one field is provided
    if request.enabled is None and request.config is None:
        raise BadRequestError(
            message="At least one field must be provided",
            data={"allowed_fields": ["enabled", "config"]},
        )

    # 云端同步来的技能 / 智能体登记在本机登记表里，业务库没有对应行，写目录覆盖
    # 不会生效也读不回来。启停就写它——读写同源，运行时解析读的也是这份。
    if request.enabled is not None and normalized_kind in ("skill", "agent"):
        from core.capabilities import device_catalog

        if device_catalog.set_enabled(normalized_kind, id, request.enabled, user_id=str(user.user_id)):
            return success_response(
                data={
                    "kind": normalized_kind,
                    "id": id,
                    "enabled": request.enabled,
                    "config": {},
                },
                message="Capability toggle saved on this device",
            )

    # 云端下发的连接器不在本机目录里，它是在目录解析之后按 mcp.json 的托管标志
    # 追加回来的——所以启停只有写进那里才作数，写目录覆盖不会生效。
    if normalized_kind == "mcp" and request.enabled is not None:
        from core.services.desktop_cloud_bridge import set_managed_connector_enabled

        if set_managed_connector_enabled(id, request.enabled):
            return success_response(
                data={"kind": normalized_kind, "id": id, "enabled": request.enabled, "config": {}},
                message="Connector toggle saved on this device",
            )

    # KB toggles are not persisted in DB catalog_overrides (no kb enum in schema).
    # Frontend persists UI preference locally; backend accepts request for API uniformity.
    if normalized_kind == "kb":
        return success_response(
            data={
                "kind": normalized_kind,
                "id": id,
                "enabled": True if request.enabled is None else request.enabled,
                "config": request.config or {},
            },
            message="Knowledge base toggle accepted (runtime-only)",
        )

    # Get runtime catalog to verify static and DB-managed public items.
    base_catalog = get_runtime_catalog(db, include_runtime_details=False)
    kind_bucket = (
        "skills"
        if normalized_kind == "skill"
        else "agents" if normalized_kind == "agent" else "mcp"
    )
    base_items = base_catalog.get(kind_bucket, [])

    item_exists = any(item.get("id") == id for item in base_items)
    # Private items (skill/mcp self-added by a user) are not in the global catalog, but their owner may still override enable/disable
    if not item_exists and not _is_owned_capability(db, user.user_id, normalized_kind, id):
        raise BadRequestError(
            message="Item not found in catalog",
            data={
                "kind": normalized_kind,
                "item_id": id,
                "hint": f"The item may not exist in the {kind_bucket} catalog",
            },
        )

    # Get current override or default values
    catalog_service = CatalogService(db)
    user_overrides = catalog_service.get_user_overrides(user.user_id, normalized_kind)

    # Find current item config
    current_enabled = True
    current_config = {}
    admin_disabled = False

    # Get from base catalog
    for item in base_items:
        if item.get("id") == id:
            current_enabled = item.get("enabled", True)
            current_config = item.get("config", {})
            admin_disabled = not bool(item.get("enabled", True))
            break

    # Admin lock: if disabled at the catalog level, user cannot re-enable
    if admin_disabled and request.enabled is True:
        raise BadRequestError(
            message="此功能已被管理员禁用，无法启用", data={"kind": normalized_kind, "item_id": id}
        )

    # Override with user settings if exists
    override_key = (
        "skills"
        if normalized_kind == "skill"
        else "agents" if normalized_kind == "agent" else "mcps"
    )
    for override_item in user_overrides.get(override_key, []):
        if override_item.get("id") == id:
            current_enabled = override_item.get("enabled", current_enabled)
            current_config = override_item.get("config", current_config)
            break

    # Apply updates
    new_enabled = request.enabled if request.enabled is not None else current_enabled
    new_config = current_config.copy()
    if request.config is not None:
        new_config.update(request.config)

    # Save override
    catalog_service.update_override(
        user_id=user.user_id,
        kind=normalized_kind,
        item_id=id,
        enabled=new_enabled,
        config=new_config if new_config else None,
    )

    # Invalidate prompt cache: tool/skill changes affect the system prompt
    try:
        from prompts.prompt_runtime import invalidate_prompt_cache

        invalidate_prompt_cache()
    except Exception:
        pass
    # Invalidate this user's capability cache so the toggle takes effect on the
    # next message (otherwise resolve_all_runtime_enabled's 30s cache hides it).
    try:
        from core.config.catalog_resolver import invalidate_capability_cache

        invalidate_capability_cache(str(user.user_id))
    except Exception:
        pass

    return success_response(
        data={"kind": normalized_kind, "id": id, "enabled": new_enabled, "config": new_config},
        message="Catalog item updated successfully",
    )
