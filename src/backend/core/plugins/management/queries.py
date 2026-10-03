"""Plugin queries responsibilities."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.db.models import AdminMcpServer, AdminSkill, InstalledPlugin
from core.infra.exceptions import BadRequestError, ResourceNotFoundError
from core.plugins.management import admin_config as plugin_admin_config
from core.plugins.packaging import sources as plugin_sources
from core.services.marketplace_service import _strip_frontmatter
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def list_installed(
    db: Session, owner_user_id: Optional[str], *, include_global: bool = False
) -> List[Dict[str, Any]]:
    """Installed plugins with personal ``enabled`` and availability ``callable`` flags.

    ``enabled`` controls ambient use. ``callable`` reports whether an
    installed component can be selected explicitly, ignoring enabled switches
    while retaining dependency and marketplace suspension checks.

    - owner_user_id=None: global plugins only (admin view).
    - owner_user_id=<user> + include_global=False: that user's private ones only.
    - owner_user_id=<user> + include_global=True: user private + admin global
      plugins (frontend user view; global items are read-only — consistent
      with the skill library showing global skills).
    """
    from core.capabilities import device_catalog
    from sqlalchemy import or_

    if device_catalog.active():
        # 混合模式：装什么由云端账号定。本机业务库里的行是历史遗留（早期版本的本机
        # 引导、或安装请求还打在本机的那阵子装的），它们的连接器指向本机没人监听的
        # 端口，跑不通，列出来只会和云端同步下来的同名插件并排。不删行——机器切回
        # 纯本机模式时它们照旧生效。
        return []

    q = db.query(InstalledPlugin)
    if owner_user_id is None:
        q = q.filter(InstalledPlugin.owner_user_id.is_(None))
    elif include_global:
        q = q.filter(
            or_(
                InstalledPlugin.owner_user_id == owner_user_id,
                InstalledPlugin.owner_user_id.is_(None),
            )
        )
    else:
        q = q.filter(InstalledPlugin.owner_user_id == owner_user_id)
    rows = q.order_by(InstalledPlugin.created_at.desc()).all()
    # Dedup: in the user view (include_global), if the same plugin (slug) was
    # both installed globally by an admin and privately by the user, keep only
    # the admin global version (global read-only takes precedence) — otherwise
    # the plugin library would show two same-named plugins (global
    # install_id=slug@global, private slug@<uid>; the ids differ so both made
    # the list).
    if owner_user_id is not None and include_global:
        global_slugs = {r.slug for r in rows if r.owner_user_id is None}
        rows = [r for r in rows if r.owner_user_id is None or r.slug not in global_slugs]
    all_skill_ids: set = set()
    all_mcp_ids: set = set()
    for r in rows:
        cids = r.component_ids or {}
        all_skill_ids.update(plugin_sources._component_keys(cids, "skills"))
        all_mcp_ids.update(plugin_sources._component_keys(cids, "mcp"))

    callable_skills = (
        {
            row[0]
            for row in db.query(AdminSkill.skill_id)
            .filter(
                AdminSkill.skill_id.in_(all_skill_ids),
                AdminSkill.dep_status == "ready",
            )
            .all()
        }
        if all_skill_ids
        else set()
    )
    callable_mcps = (
        {
            row[0]
            for row in db.query(AdminMcpServer.server_id)
            .filter(
                AdminMcpServer.server_id.in_(all_mcp_ids),
            )
            .all()
        }
        if all_mcp_ids
        else set()
    )

    if all_mcp_ids:
        from core.db.models import McpMarketInstallation

        suspended = {
            sid
            for (sid,) in db.query(McpMarketInstallation.server_id)
            .filter(
                McpMarketInstallation.server_id.in_(all_mcp_ids),
                McpMarketInstallation.status == "suspended",
            )
            .all()
        }
        callable_mcps.difference_update(suspended)

    # Enabled state:
    # - user view (owner_user_id non-empty): determined by the user's
    #   "effectively enabled" set (including per-user overrides), so the user's
    #   personal toggles on global plugins are reflected correctly.
    # - admin view (owner_user_id empty): determined by components' global is_enabled.
    if owner_user_id is not None:
        from core.config.catalog_resolver import resolve_all_runtime_enabled

        eff_skills, _eff_agents, eff_mcps = resolve_all_runtime_enabled(db, owner_user_id)
        enabled_skills = set(eff_skills or [])
        enabled_mcps = set(eff_mcps or [])
    else:
        enabled_skills = (
            {
                row[0]
                for row in db.query(AdminSkill.skill_id)
                .filter(AdminSkill.skill_id.in_(all_skill_ids), AdminSkill.is_enabled.is_(True))
                .all()
            }
            if all_skill_ids
            else set()
        )
        enabled_mcps = (
            {
                row[0]
                for row in db.query(AdminMcpServer.server_id)
                .filter(
                    AdminMcpServer.server_id.in_(all_mcp_ids), AdminMcpServer.is_enabled.is_(True)
                )
                .all()
            }
            if all_mcp_ids
            else set()
        )

    component_tools = {}
    if all_mcp_ids:
        for row in db.query(AdminMcpServer).filter(AdminMcpServer.server_id.in_(all_mcp_ids)).all():
            component_tools[row.server_id] = [
                t["name"]
                for t in (row.tools_json or [])
                if isinstance(t, dict) and isinstance(t.get("name"), str)
            ]

    out: List[Dict[str, Any]] = []
    for r in rows:
        cids = r.component_ids or {}
        enabled = any(
            s in enabled_skills for s in (plugin_sources._component_keys(cids, "skills"))
        ) or any(m in enabled_mcps for m in (plugin_sources._component_keys(cids, "mcp")))
        callable_now = any(
            s in callable_skills for s in (plugin_sources._component_keys(cids, "skills"))
        ) or any(m in callable_mcps for m in (plugin_sources._component_keys(cids, "mcp")))
        item = _installed_to_dict(r, enabled=enabled, callable_now=callable_now)
        item["tools"] = sorted(
            {name for mid in item["mcp"] for name in component_tools.get(mid, [])}
        )
        out.append(item)
    return out


def _installed_to_dict(
    r: InstalledPlugin,
    *,
    enabled: bool = True,
    callable_now: bool = True,
) -> Dict[str, Any]:
    cids = r.component_ids or {}
    return {
        "install_id": r.install_id,
        "slug": r.slug,
        "name": r.name,
        "is_global": r.owner_user_id is None,  # admin global install (read-only on the frontend)
        "version": r.version,
        "description": r.description or "",
        "category": r.category or "",
        "icon": r.icon,
        "source": r.source,
        "enabled": enabled,
        # Hard runtime availability, independent of the current user's
        # personal catalog switch. Explicit pickers hide false entries.
        "callable": callable_now,
        "skills": plugin_sources._component_keys(cids, "skills"),
        "mcp": plugin_sources._component_keys(cids, "mcp"),
        "import_report": r.import_report or {},
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "has_admin_config": plugin_admin_config._has_admin_config_for_slug(r.slug),
    }


def get_installed_detail(
    db: Session, install_id: str, *, owner_user_id: Optional[str]
) -> Dict[str, Any]:
    """Full detail of an installed plugin: skills (with instructions/file list) + MCP (with tool list).

    Powers the frontend's three-level drill-in: "open plugin → view components
    → open a single skill/MCP for details".
    """
    row = db.query(InstalledPlugin).filter(InstalledPlugin.install_id == install_id).first()
    if row is None:
        raise ResourceNotFoundError("installed_plugin", install_id)
    # Details are viewable for one's own private plugins, or admin global plugins (empty owner, viewable by everyone).
    if row.owner_user_id is not None and row.owner_user_id != owner_user_id:
        raise BadRequestError(message="无权查看该插件")

    cids = row.component_ids or {}
    skill_ids = plugin_sources._component_keys(cids, "skills")
    server_ids = plugin_sources._component_keys(cids, "mcp")

    # Component enabled state: user view uses their "effectively enabled" set (including per-user overrides); admin view uses is_enabled.
    eff_skills: Optional[set] = None
    eff_mcps: Optional[set] = None
    if owner_user_id is not None:
        from core.config.catalog_resolver import resolve_all_runtime_enabled

        es, _ea, em = resolve_all_runtime_enabled(db, owner_user_id)
        eff_skills, eff_mcps = set(es or []), set(em or [])

    skills_out: List[Dict[str, Any]] = []
    if skill_ids:
        for s in db.query(AdminSkill).filter(AdminSkill.skill_id.in_(skill_ids)).all():
            extra = s.extra_files or {}
            # secrets.json is not shown as an ordinary file
            files = sorted(k for k in extra.keys() if k != "secrets.json")
            sk_enabled = (
                (s.skill_id in eff_skills) if eff_skills is not None else bool(s.is_enabled)
            )
            skills_out.append(
                {
                    "skill_id": s.skill_id,
                    "name": s.display_name or s.skill_id,
                    "description": s.description or "",
                    "version": s.version or "",
                    "tags": list(s.tags or []),
                    "enabled": sk_enabled,
                    "instructions": _strip_frontmatter(s.skill_content),
                    "files": files,
                    "has_secrets": "secrets.json" in extra,
                }
            )

    mcp_out: List[Dict[str, Any]] = []
    if server_ids:
        for m in db.query(AdminMcpServer).filter(AdminMcpServer.server_id.in_(server_ids)).all():
            raw_tools = m.tools_json or []
            tools = [
                {
                    "name": str(tdef.get("name") or ""),
                    "description": str(tdef.get("description") or ""),
                }
                for tdef in raw_tools
                if isinstance(tdef, dict)
            ]
            mc_enabled = (m.server_id in eff_mcps) if eff_mcps is not None else bool(m.is_enabled)
            mcp_out.append(
                {
                    "server_id": m.server_id,
                    "name": m.display_name or m.server_id,
                    "description": m.description or "",
                    "transport": m.transport,
                    "url": m.url,
                    "enabled": mc_enabled,
                    "needs_runtime": m.transport == "stdio",
                    "tools": tools,
                }
            )

    return {
        "install_id": row.install_id,
        "slug": row.slug,
        "name": row.name,
        "is_global": row.owner_user_id is None,
        "version": row.version,
        "description": row.description or "",
        "category": row.category or "",
        "icon": row.icon,
        "source": row.source,
        "import_report": row.import_report or {},
        "skills": skills_out,
        "mcp": mcp_out,
        # Current admin-level config state (user side is read-only: returns only whether each field is set + overall readiness, never real values)
        "admin_config": plugin_admin_config._admin_config_view(
            plugin_admin_config._admin_config_for_slug(row.slug), with_values=False
        ),
        # Account connection type (dingtalk / lark / None): the frontend uses this to render the account-connection panel on the detail page
        "connection": plugin_admin_config._connection_for_slug(row.slug),
    }
