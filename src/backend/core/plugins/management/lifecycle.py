"""Plugin lifecycle responsibilities."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from core.db.models import AdminMcpServer, AdminSkill, InstalledPlugin
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.infra.time import utc_now
from core.plugins.management import market as plugin_market
from core.plugins.management import projection as plugin_projection
from core.plugins.packaging import sources as plugin_sources
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def uninstall_plugin(
    db: Session, install_id: str, *, owner_user_id: Optional[str]
) -> Dict[str, Any]:
    """Uninstall a plugin: precisely reverse-delete skills/MCP by source_plugin + owner, then delete the installation record."""
    row = db.query(InstalledPlugin).filter(InstalledPlugin.install_id == install_id).first()
    if row is None:
        raise ResourceNotFoundError("installed_plugin", install_id)
    # Permissions: private plugins can only be uninstalled by their owner; global plugins require owner_user_id None (the route layer guarantees admin)
    if row.owner_user_id != owner_user_id:
        raise BadRequestError(message="无权卸载该插件")

    slug = row.slug

    def _owner_filter(model):
        return (
            (model.owner_user_id == owner_user_id)
            if owner_user_id is not None
            else model.owner_user_id.is_(None)
        )

    removed_skill_ids = [
        sid
        for (sid,) in db.query(AdminSkill.skill_id).filter(
            AdminSkill.source_plugin == slug, _owner_filter(AdminSkill)
        )
    ]
    n_sk = (
        db.query(AdminSkill)
        .filter(AdminSkill.source_plugin == slug, _owner_filter(AdminSkill))
        .delete(synchronize_session=False)
    )
    n_mcp = (
        db.query(AdminMcpServer)
        .filter(AdminMcpServer.source_plugin == slug, _owner_filter(AdminMcpServer))
        .delete(synchronize_session=False)
    )

    db.delete(row)
    db.commit()
    plugin_projection._purge_sandbox_skill_files(removed_skill_ids)
    plugin_projection._refresh_after_change(owner_user_id)
    plugin_projection._remove_plugin_from_store(slug)
    logger.info("plugin_uninstalled: id=%s slug=%s skills=%d mcp=%d", install_id, slug, n_sk, n_mcp)
    return {"install_id": install_id, "slug": slug, "removed_skills": n_sk, "removed_mcp": n_mcp}


def set_plugin_enabled(
    db: Session, install_id: str, *, enabled: bool, owner_user_id: Optional[str]
) -> Dict[str, Any]:
    """Toggle a plugin as a whole: bulk-flip is_enabled on all of its skills/MCP.

    stdio MCP (non-empty command, no url) stays disabled even when enabling —
    it needs the runtime to be fully in place.
    """
    row = db.query(InstalledPlugin).filter(InstalledPlugin.install_id == install_id).first()
    if row is None:
        raise ResourceNotFoundError("installed_plugin", install_id)
    if row.owner_user_id != owner_user_id:
        raise BadRequestError(message="无权操作该插件")

    cids = row.component_ids or {}
    skill_ids = plugin_sources._component_keys(cids, "skills")
    server_ids = plugin_sources._component_keys(cids, "mcp")
    if skill_ids:
        db.query(AdminSkill).filter(AdminSkill.skill_id.in_(skill_ids)).update(
            {AdminSkill.is_enabled: enabled}, synchronize_session=False
        )
    if server_ids:
        # Query all MCP servers at once, then decide row by row for stdio (needs runtime) — stdio stays disabled even when the plugin as a whole is enabled.
        for srv in db.query(AdminMcpServer).filter(AdminMcpServer.server_id.in_(server_ids)).all():
            srv.is_enabled = bool(enabled) and srv.transport != "stdio"
    db.commit()
    plugin_projection._refresh_after_change(owner_user_id)
    return {"install_id": install_id, "enabled": enabled}


def set_plugin_component_enabled(
    db: Session,
    install_id: str,
    *,
    kind: str,
    component_id: str,
    enabled: bool,
    owner_user_id: Optional[str],
) -> Dict[str, Any]:
    """Individually toggle the global is_enabled of one component (skill / MCP) inside a plugin.

    Used by the plugin detail page's "per-skill/MCP management" — plugin skills
    no longer appear in the "Skill Management" list and are managed here
    instead. component_id must belong to this plugin's component_ids (prevents
    privilege escalation into modifying other plugins / hand-created skills).
    stdio MCP stays disabled even when enabling (needs the runtime fully in
    place), consistent with the whole-plugin toggle.
    """
    if kind not in ("skill", "mcp"):
        raise BadRequestError(message=f"不支持的组件类型：{kind}")
    row = db.query(InstalledPlugin).filter(InstalledPlugin.install_id == install_id).first()
    if row is None:
        raise ResourceNotFoundError("installed_plugin", install_id)
    if row.owner_user_id != owner_user_id:
        raise BadRequestError(message="无权操作该插件")

    cids = row.component_ids or {}
    if kind == "skill":
        if component_id not in (plugin_sources._component_keys(cids, "skills")):
            raise BadRequestError(message="该技能不属于此插件")
        sk = db.query(AdminSkill).filter(AdminSkill.skill_id == component_id).first()
        if sk is None:
            raise ResourceNotFoundError("admin_skill", component_id)
        sk.is_enabled = bool(enabled)
        effective = sk.is_enabled
    else:
        if component_id not in (plugin_sources._component_keys(cids, "mcp")):
            raise BadRequestError(message="该 MCP 不属于此插件")
        srv = db.query(AdminMcpServer).filter(AdminMcpServer.server_id == component_id).first()
        if srv is None:
            raise ResourceNotFoundError("admin_mcp_server", component_id)
        # stdio MCP stays disabled even when enabling — needs the runtime fully in place.
        srv.is_enabled = bool(enabled) and srv.transport != "stdio"
        effective = srv.is_enabled
    db.commit()
    plugin_projection._refresh_after_change(owner_user_id)
    return {
        "install_id": install_id,
        "kind": kind,
        "component_id": component_id,
        "enabled": effective,
    }


def set_plugin_enabled_for_user(
    db: Session, install_id: str, *, enabled: bool, user_id: str
) -> Dict[str, Any]:
    """A frontend user enables/disables a plugin **for themself** (including admin global plugins).

    Writes a per-user catalog override to each component (kind=skill/mcp)
    without touching the component's global is_enabled — so a user's toggle on
    a global plugin affects only them; it works the same for their own private
    plugins (_owned_enabled_ids honors overrides). Globally disabled components
    (e.g. stdio MCP) are protected by _merge_kind's admin-lock, so users cannot
    enable them beyond their privileges.
    """
    row = db.query(InstalledPlugin).filter(InstalledPlugin.install_id == install_id).first()
    if row is None:
        raise ResourceNotFoundError("installed_plugin", install_id)
    # Allowed for one's own private plugin, or an admin global plugin (empty owner; users may toggle it for themselves).
    if row.owner_user_id is not None and row.owner_user_id != user_id:
        raise BadRequestError(message="无权操作该插件")

    from core.services.catalog_service import CatalogService

    svc = CatalogService(db)
    cids = row.component_ids or {}
    for sid in plugin_sources._component_keys(cids, "skills"):
        svc.update_override(user_id, "skill", sid, enabled)
    for sid in plugin_sources._component_keys(cids, "mcp"):
        svc.update_override(user_id, "mcp", sid, enabled)

    plugin_projection._refresh_after_change(user_id)
    return {"install_id": install_id, "enabled": enabled}


def set_plugin_component_enabled_for_user(
    db: Session,
    install_id: str,
    *,
    kind: str,
    component_id: str,
    enabled: bool,
    user_id: str,
) -> Dict[str, Any]:
    """A frontend user enables/disables **one component** of a plugin for themself.

    Per-component counterpart of ``set_plugin_enabled_for_user``: writes a
    per-user catalog override instead of flipping the component's global
    ``is_enabled``, so it composes with the whole-plugin user toggle and stays
    invisible to other users. ``component_id`` must belong to this plugin's
    ``component_ids`` (prevents escalating into other plugins' components),
    matching the admin-side ``set_plugin_component_enabled`` guard.
    """
    if kind not in ("skill", "mcp"):
        raise BadRequestError(message=f"不支持的组件类型：{kind}")
    row = db.query(InstalledPlugin).filter(InstalledPlugin.install_id == install_id).first()
    if row is None:
        raise ResourceNotFoundError("installed_plugin", install_id)
    if row.owner_user_id is not None and row.owner_user_id != user_id:
        raise BadRequestError(message="无权操作该插件")

    cids = row.component_ids or {}
    pool = plugin_sources._component_keys(cids, "skills" if kind == "skill" else "mcp")
    if component_id not in (pool or []):
        raise BadRequestError(message="该组件不属于此插件")

    from core.services.catalog_service import CatalogService

    CatalogService(db).update_override(user_id, kind, component_id, enabled)
    plugin_projection._refresh_after_change(user_id)
    return {
        "install_id": install_id,
        "kind": kind,
        "component_id": component_id,
        "enabled": enabled,
    }


def set_installed_plugin_meta(
    db: Session,
    install_id: str,
    *,
    owner_user_id: Optional[str],
    display_name: Optional[str] = None,
    category: Optional[str] = None,
    icon: Optional[str] = None,
) -> Dict[str, Any]:
    """Edit an installed plugin's display metadata (display_name/category/icon).

    Display metadata is UI configuration, not manifest data: a user edits their
    own imported/private plugins here; global installs are edited by the admin
    (owner_user_id=None via the admin route). Only provided fields change; an
    empty icon/category clears it, display_name never becomes empty.
    """
    row = db.query(InstalledPlugin).filter(InstalledPlugin.install_id == install_id).first()
    if row is None:
        raise ResourceNotFoundError("installed_plugin", install_id)
    if row.owner_user_id != owner_user_id:
        raise BadRequestError(message="无权修改该插件")
    if display_name is not None and display_name.strip():
        row.name = display_name.strip()
    if category is not None:
        row.category = category.strip()
    if icon is not None:
        row.icon = plugin_market._validate_icon(icon) or None
    row.updated_at = utc_now()
    db.commit()
    return {
        "install_id": row.install_id,
        "slug": row.slug,
        "name": row.name,
        "category": row.category or "",
        "icon": row.icon,
    }
