"""Business database catalog ownership and component visibility queries."""
import logging
from typing import Any, Dict, List
logger = logging.getLogger(__name__)

def _load_owned_capability_items(db, user_id: str) -> tuple:
    """Load a user's self-added private skills / MCPs and convert them to catalog items (owner='self').

    Returns (skill_items, mcp_items). These items are not in the global catalog and are
    injected only into that user's /v1/catalog response; the frontend shows the "mine"
    badge and delete button based on ``owner == 'self'``.
    """
    from core.config.catalog_details import resolve_skill_detail
    from core.db.models import AdminMcpServer, AdminSkill, McpMarketInstallation
    from core.services.skill_icon_service import get_skill_icons

    skill_items: List[Dict[str, Any]] = []
    mcp_items: List[Dict[str, Any]] = []
    icons = get_skill_icons(db)
    try:
        marketplace_server_ids = {
            str(row[0])
            for row in db.query(McpMarketInstallation.server_id)
            .filter(McpMarketInstallation.owner_user_id == user_id)
            .all()
        }
        for row in (
            db.query(AdminSkill)
            .filter(AdminSkill.owner_user_id == user_id)
            .order_by(AdminSkill.updated_at.desc())
            .all()
        ):
            # When user_intro is unset, fall back to showing the SKILL.md body (same policy as the global catalog).
            detail = resolve_skill_detail(row.user_intro, row.skill_content or "")
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
                    "created_at": row.created_at.isoformat() if row.created_at else None,
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
            extra_config = dict(row.extra_config or {})
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
                    "tools": [
                        t["name"]
                        for t in (row.tools_json or [])
                        if isinstance(t, dict) and isinstance(t.get("name"), str)
                    ],
                    "detail": row.user_intro or "",
                    "owner": "self",
                    "deletable": True,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "marketplace_installed": bool(extra_config.get("market_slug"))
                    or row.server_id in marketplace_server_ids,
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
    2. **Built-in bundle scan**: skills/MCP provided by
       ``plugin_bundles/{default,marketplace}/*`` (derived from each bundle's
       ``skills/*/`` dirs + MCP declarations; the Agent Plugins standard manifest
       has no ``components`` list) — MCPs of built-in plugins (e.g. automation /
       skill-manager) go through ``_ports.py`` → catalog.json and statically
       bubble up as first-class entries; the DB has no ``source_plugin`` row
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
        from core.plugins.management import builtin_plugin_component_ids

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
