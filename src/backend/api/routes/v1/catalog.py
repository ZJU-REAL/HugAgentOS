"""Catalog management API routes (v1)."""

import logging
from threading import Lock
from time import monotonic
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Depends, Path
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from core.db.engine import get_db
from core.auth.backend import get_current_user, UserContext
from core.db.repository import KBRepository
from core.services import CatalogService
from core.infra.responses import success_response
from core.infra.exceptions import BadRequestError
from core.config.catalog_runtime import get_runtime_catalog
from core.kb.dify_kb import is_dify_enabled, list_datasets

# ── Dify dataset list cache (avoids 3s timeout on every page load) ──
_dify_cache_lock = Lock()
_dify_cache: Optional[tuple] = None  # (expires_at, items)
_DIFY_CACHE_TTL = 30.0


def _list_datasets_cached() -> List[Dict[str, Any]]:
    """Return Dify datasets with 30s in-memory cache."""
    global _dify_cache
    now = monotonic()
    with _dify_cache_lock:
        if _dify_cache is not None:
            expires_at, items = _dify_cache
            if now < expires_at:
                return items

    items = list_datasets(page=1, limit=100)
    with _dify_cache_lock:
        _dify_cache = (now + _DIFY_CACHE_TTL, items)
    return items


router = APIRouter(prefix="/v1/catalog", tags=["Catalog"])
logger = logging.getLogger(__name__)


def _load_owned_capability_items(db, user_id: str) -> tuple:
    """Load a user's self-added private skills / MCPs and convert them to catalog items (owner='self').

    Returns (skill_items, mcp_items). These items are not in the global catalog and are
    injected only into that user's /v1/catalog response; the frontend shows the "mine"
    badge and delete button based on ``owner == 'self'``.
    """
    from core.config.catalog_loader import skill_body_from_raw
    from core.db.models import AdminMcpServer, AdminSkill
    from core.services.skill_icon_service import get_skill_icons

    skill_items: List[Dict[str, Any]] = []
    mcp_items: List[Dict[str, Any]] = []
    icons = get_skill_icons(db)
    try:
        for row in (
            db.query(AdminSkill)
            .filter(AdminSkill.owner_user_id == user_id)
            .order_by(AdminSkill.updated_at.desc())
            .all()
        ):
            # When user_intro is unset, fall back to showing the SKILL.md body (same policy as the global catalog).
            detail = row.user_intro or skill_body_from_raw(row.skill_content or "")
            skill_items.append(
                {
                    "id": row.skill_id,
                    "kind": "tool_bundle",
                    "name": row.display_name,
                    "description": row.description or "",
                    "desc": row.description or "",
                    "enabled": bool(row.is_enabled),
                    "version": row.version or "1.0.0",
                    "config": {"tags": row.tags or []},
                    "tags": row.tags or [],
                    "detail": detail,
                    "icon": icons.get(row.skill_id, ""),
                    "owner": "self",
                    "deletable": True,
                }
            )
    except Exception as exc:
        logger.warning("Failed to load owned skills for %s: %s", user_id, exc)

    try:
        for row in (
            db.query(AdminMcpServer)
            .filter(AdminMcpServer.owner_user_id == user_id)
            .order_by(AdminMcpServer.sort_order)
            .all()
        ):
            mcp_items.append(
                {
                    "id": row.server_id,
                    "kind": "mcp_server",
                    "name": row.display_name,
                    "description": row.description or "",
                    "desc": row.description or "",
                    "enabled": bool(row.is_enabled),
                    "version": "1",
                    "config": {"server": row.server_id},
                    "icon": row.icon or "",
                    "detail": row.user_intro or "",
                    "owner": "self",
                    "deletable": True,
                }
            )
    except Exception as exc:
        logger.warning("Failed to load owned MCP servers for %s: %s", user_id, exc)

    return skill_items, mcp_items


def _plugin_component_ids(db) -> tuple:
    """Skill / MCP id sets of plugin components, used to remove them from the skill / MCP tool libraries so they show only under "Plugins".

    Union of two sources — both are required:
    1. **DB install source**: ``AdminSkill/AdminMcpServer.source_plugin`` is non-null —
       written dynamically when a user installs a plugin.
    2. **Built-in manifest declaration**: ``components`` in
       ``plugin_bundles/{default,marketplace}/*/plugin.json`` — MCPs of built-in plugins
       (e.g. automation / skill-manager) go through ``_ports.py`` → catalog.json and
       statically bubble up as first-class entries; the DB has no ``source_plugin`` row
       for them, so source 1 alone cannot remove them.

    Filters **display** only; does not affect the enablement resolution of
    ``resolve_all_runtime_enabled`` (agents can still use them as usual).
    """
    from core.db.models import AdminMcpServer, AdminSkill

    skill_ids: set = set()
    mcp_ids: set = set()
    try:
        skill_ids = {
            r[0]
            for r in db.query(AdminSkill.skill_id)
            .filter(AdminSkill.source_plugin.isnot(None))
            .all()
        }
        mcp_ids = {
            r[0]
            for r in db.query(AdminMcpServer.server_id)
            .filter(AdminMcpServer.source_plugin.isnot(None))
            .all()
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("plugin component id load failed: %s", exc)
    try:
        from core.services.plugin_service import builtin_plugin_component_ids

        fs_skill_ids, fs_mcp_ids = builtin_plugin_component_ids()
        skill_ids |= fs_skill_ids
        mcp_ids |= fs_mcp_ids
    except Exception as exc:  # noqa: BLE001
        logger.warning("builtin plugin component id scan failed: %s", exc)
    return skill_ids, mcp_ids


def _is_owned_capability(db, user_id: str, kind: str, item_id: str) -> bool:
    """Whether the item is a private skill/mcp self-added by the current user (owner == user_id)."""
    from core.db.models import AdminMcpServer, AdminSkill

    try:
        if kind == "skill":
            return (
                db.query(AdminSkill)
                .filter(AdminSkill.skill_id == item_id, AdminSkill.owner_user_id == user_id)
                .first()
                is not None
            )
        if kind == "mcp":
            return (
                db.query(AdminMcpServer)
                .filter(
                    AdminMcpServer.server_id == item_id, AdminMcpServer.owner_user_id == user_id
                )
                .first()
                is not None
            )
    except Exception:
        return False
    return False


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


@router.get("", summary="获取能力目录")
async def get_catalog_items(
    user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)
):
    """获取完整能力目录，含技能（skills）、子智能体（agents）、MCP 服务及知识库（KB）。

    在系统默认配置的基础上叠加当前用户的个性化覆盖：每项的启用状态与配置优先返回
    用户自定义值，否则用默认值；管理员在 catalog.json 中禁用的项对前台完全隐藏。
    KB 包含公共库（Dify）与当前用户的私有库（本地 Milvus）。
    """
    # Opportunistically clear this user's capability-resolution cache: on the agent side,
    # resolve_all_runtime_enabled has a 30s in-process cache, and it is cross-process (the
    # mcp container changing the DB cannot touch the backend process's cache). The frontend
    # re-fetches this endpoint after a skill is created/installed/deleted (including when
    # the agent triggers it via the skill-manager plugin's MCP tools) — piggyback on this
    # request within the backend process to clear the cache, so a "just-created skill"
    # takes effect for the agent on the very next message, avoiding up to 30s of it being
    # invisible/unusable. The catalog itself queries the DB directly and does not use this
    # cache, so this call only affects agent resolution and is harmless to display.
    try:
        from core.config.catalog_resolver import invalidate_capability_cache

        invalidate_capability_cache(str(user.user_id))
    except Exception as exc:  # noqa: BLE001
        logger.debug("catalog GET: invalidate_capability_cache failed: %s", exc)

    # Static catalog.json + public DB-managed capabilities.
    base_catalog = get_runtime_catalog(db)

    # Get user overrides (graceful degradation if table doesn't exist yet)
    try:
        catalog_service = CatalogService(db)
        user_overrides = catalog_service.get_user_overrides(user.user_id)
    except Exception as exc:
        logger.warning("Failed to load user catalog overrides: %s", exc)
        user_overrides = {"skills": [], "agents": [], "mcps": []}

    # Merge base catalog with user overrides
    def merge_items(base_items: List[Dict], override_items: List[Dict]) -> List[Dict]:
        """Merge base catalog items with user overrides.

        Admin lock rule: if an item is disabled in the base catalog
        (catalog.json enabled=false), user overrides cannot re-enable it.
        """
        # Create a map of overrides by id
        override_map = {item["id"]: item for item in override_items}

        result = []
        for base_item in base_items:
            item_id = base_item.get("id")
            base_enabled = bool(base_item.get("enabled", True))

            # Admin-disabled items are completely hidden from user frontend
            if not base_enabled:
                continue

            # Start with base item
            merged = dict(base_item)

            # Apply user override if exists
            if item_id in override_map:
                override = override_map[item_id]
                merged["enabled"] = override["enabled"]
                if override.get("config"):
                    # Merge configs
                    base_config = merged.get("config", {})
                    override_config = override.get("config", {})
                    merged["config"] = {**base_config, **override_config}

            config = merged.get("config", {})
            if isinstance(config, dict):
                tags = config.get("tags")
                if isinstance(tags, list):
                    merged["tags"] = [str(tag).strip() for tag in tags if str(tag).strip()]
                server = config.get("server")
                if isinstance(server, str) and server.strip():
                    merged["server"] = server.strip()

            result.append(merged)

        return result

    # ── Private skills / MCPs self-added by the current user (owner-isolated, not in the global catalog) ──
    owned_skill_items, owned_mcp_items = _load_owned_capability_items(db, user.user_id)

    # Dedupe: if a user's privately installed skill / MCP has been promoted to global by
    # an admin (same underlying entry_name), hide the private duplicate and keep only the
    # global version — otherwise the capability library would show two identically named,
    # identical-content items (private id=entry-fingerprint, global id=entry, which differ
    # so both entered the list). Merge by entry_name; dedupe only against enabled global items.
    from core.services.marketplace_service import base_entry_name

    for kind, owned in (("skills", owned_skill_items), ("mcp", owned_mcp_items)):
        global_ids = {it.get("id") for it in base_catalog.get(kind, []) if it.get("enabled", True)}
        owned[:] = [it for it in owned if base_entry_name(it["id"], user.user_id) not in global_ids]

    # Merge each category
    mcp_items = merge_items(
        base_catalog.get("mcp", []) + owned_mcp_items, user_overrides.get("mcps", [])
    )

    # ── Public KB (Dify) ──────────────────────────────────────────────────────
    public_kb_items: List[Dict[str, Any]] = []
    if is_dify_enabled():
        try:
            dify_items = _list_datasets_cached()
            # Permission assignment: narrow to datasets visible to the current user (public + granted scoped; defaults to public when unset).
            from core.auth.kb_permissions import get_dataset_levels

            ds_ids = [str(it.get("id", "")).strip() for it in dify_items if it.get("id")]
            ds_levels = get_dataset_levels(db, user.user_id, ds_ids)
            for item in dify_items:
                ds_id = str(item.get("id", "")).strip()
                level = ds_levels.get(ds_id)
                if not level:
                    continue
                item["visibility"] = "public"
                item["is_public"] = True
                item["access_level"] = level
                public_kb_items.append(item)
        except Exception as exc:
            logger.warning("Failed to load Dify KB datasets: %s", exc)

    # ── Private KB (local Milvus) ─────────────────────────────────────────────
    try:
        kb_repo = KBRepository(db)
        spaces = kb_repo.list_spaces(user.user_id)
    except Exception as exc:
        logger.warning("Failed to load private KB spaces: %s", exc)
        spaces = []
    private_kb_items: List[Dict[str, Any]] = []
    for space in spaces:
        extra = space.extra_data if isinstance(space.extra_data, dict) else {}
        is_system_managed = bool(extra.get("system_managed"))
        if is_system_managed:
            continue
        tag = str(extra.get("tag") or "").strip()
        tags = [tag] if tag else []
        private_kb_items.append(
            {
                "id": space.kb_id,
                "kind": "knowledge_base",
                "name": space.name,
                "description": space.description or "无简介",
                "desc": space.description or "无简介",
                "enabled": True,
                "version": "local",
                "provider": "HugAgentOS-KB",
                "visibility": space.visibility,
                "is_public": space.visibility == "public",
                "chunk_method": space.chunk_method or "semantic",
                "document_count": space.document_count or 0,
                "total_size_bytes": space.total_size_bytes or 0,
                "detail": (f"### {space.name}\n\n" f"{space.description or '暂无简介'}\n"),
                "tags": tags,
                "system_managed": is_system_managed,
                "pinned": bool(extra.get("pinned")),
                "editable": not is_system_managed and extra.get("editable", True) is not False,
                "deletable": not is_system_managed and extra.get("deletable", True) is not False,
                "uploadable": not is_system_managed and extra.get("uploadable", True) is not False,
            }
        )
    private_kb_items.sort(key=lambda item: (not bool(item.get("pinned")), item.get("name", "")))

    # ── Shared KB (admin-managed local Milvus: public = everyone / scoped = specified visibility) ──
    # Single source of truth for permission assignment: show only shared spaces the current
    # user may access (public spaces + granted scoped spaces), excluding ones they own
    # (already in private_kb_items). Capability bits follow the grant level: view =
    # read-only, edit = can upload, admin = can manage. On the retrieval side, the
    # public/scoped branches of retrieve_local_kb admit the same visible set.
    public_local_kb_items: List[Dict[str, Any]] = []
    try:
        from core.auth.kb_permissions import get_accessible_local_kb_levels, level_to_caps
        from core.db.models import KBSpace

        levels = get_accessible_local_kb_levels(db, user.user_id)
        owned_ids = {s.kb_id for s in spaces}
        shared_ids = [kid for kid in levels if kid not in owned_ids]
        shared_spaces = []
        if shared_ids:
            shared_spaces = (
                db.query(KBSpace)
                .filter(
                    KBSpace.kb_id.in_(shared_ids),
                    KBSpace.deleted_at.is_(None),
                )
                .all()
            )
        for space in shared_spaces:
            extra = space.extra_data if isinstance(space.extra_data, dict) else {}
            if bool(extra.get("system_managed")):
                continue
            level = levels.get(space.kb_id, "view")
            # No longer attach "public/granted" labels by visibility (the implicit model does not expose visibility distinctions to users); keep only the admin-defined tag.
            tag = str(extra.get("tag") or "").strip()
            public_local_kb_items.append(
                {
                    "id": space.kb_id,
                    "kind": "knowledge_base",
                    "name": space.name,
                    "description": space.description or "无简介",
                    "desc": space.description or "无简介",
                    "enabled": True,
                    "version": "local",
                    "provider": "HugAgentOS-KB",
                    "visibility": space.visibility,
                    "is_public": space.visibility == "public",
                    "access_level": level,
                    "chunk_method": space.chunk_method or "semantic",
                    "document_count": space.document_count or 0,
                    "total_size_bytes": space.total_size_bytes or 0,
                    "detail": (f"### {space.name}\n\n" f"{space.description or '暂无简介'}\n"),
                    "tags": [tag] if tag else [],
                    "system_managed": False,
                    "pinned": False,
                    **level_to_caps(level),
                }
            )
    except Exception as exc:
        logger.warning("Failed to load shared KB spaces: %s", exc)
    public_local_kb_items.sort(key=lambda item: item.get("name", ""))

    kb_items: List[Dict[str, Any]] = public_kb_items + public_local_kb_items + private_kb_items

    # Plugin components are shown only under "Plugins"; remove them from the skill / MCP tool library lists (avoids duplication; does not affect enablement resolution).
    plugin_skill_ids, plugin_mcp_ids = _plugin_component_ids(db)
    skill_list = [
        it
        for it in merge_items(
            base_catalog.get("skills", []) + owned_skill_items, user_overrides.get("skills", [])
        )
        if it.get("id") not in plugin_skill_ids
    ]
    mcp_list = [it for it in mcp_items if it.get("id") not in plugin_mcp_ids]

    data = {
        "skills": skill_list,
        "agents": merge_items(base_catalog.get("agents", []), user_overrides.get("agents", [])),
        "mcp": mcp_list,
        "kb": kb_items,
    }

    return success_response(data=data, message="Catalog retrieved successfully")


@router.patch("/{kind}/{id}", summary="更新能力配置")
async def update_catalog_item(
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
