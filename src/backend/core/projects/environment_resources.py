"""Authorized capability inventory for project environments."""

from __future__ import annotations

from core.projects.environment.config import FIELDS


def _keys(entries):
    return [
        (
            str(
                e.get("id")
                or e.get("key")
                or e.get("skill_id")
                or e.get("server_id")
                or e.get("agent_id")
            )
            if isinstance(e, dict)
            else str(e)
        )
        for e in entries or []
    ]


def environment_resources(db, user_id):
    from core.capabilities.skills.loader_factory import get_skill_loader
    from core.harness.registry import device_catalog
    from core.db.models import AdminMcpServer, AdminSkill, InstalledPlugin
    from core.capabilities.agents.builtin import merge_builtin_subagents
    from core.capabilities.agents.edition import UserAgentService
    from core.auth.users import UserService
    from sqlalchemy import or_

    service = UserAgentService(db)
    resources = service.list_available_resources(owner_user_id=user_id)
    agents = merge_builtin_subagents(
        service.list_for_user(user_id),
        disabled_agent_ids=UserService(db).get_disabled_builtin_subagent_ids(user_id),
        include_disabled=True,
    )
    agents = device_catalog.merge_items(agents, device_catalog.agent_entries(), key="agent_id")
    graph = {field: {} for field in FIELDS}
    for agent in agents:
        if agent.get("is_enabled", True):
            graph["agent_ids"][agent["agent_id"]] = {
                "id": agent["agent_id"],
                "name": agent["name"],
                "description": agent.get("description", ""),
                "dependencies": {
                    "skill_ids": agent.get("skill_ids", []),
                    "mcp_ids": agent.get("mcp_server_ids", []),
                    "plugin_ids": agent.get("plugin_ids", []),
                },
            }
    metadata = get_skill_loader().load_all_metadata()
    skill_rows = {r.skill_id: r for r in db.query(AdminSkill).all()}
    for sid, meta in metadata.items():
        row = skill_rows.get(sid)
        if row and (
            row.owner_user_id not in (None, user_id)
            or (row.owner_user_id is None and not row.is_enabled)
        ):
            continue
        graph["skill_ids"][sid] = {
            "id": sid,
            "name": getattr(meta, "name", sid),
            "description": getattr(meta, "description", ""),
            "dependencies": {"mcp_ids": list(getattr(meta, "mcp_server_ids", []) or [])},
        }
    for item in resources["skills"]:
        row = skill_rows.get(item["id"])
        if row and row.owner_user_id is None and not row.is_enabled:
            continue
        graph["skill_ids"].setdefault(item["id"], {**item, "dependencies": {}})

    for row in (
        db.query(AdminMcpServer)
        .filter(
            or_(AdminMcpServer.owner_user_id == user_id, AdminMcpServer.owner_user_id.is_(None))
        )
        .all()
    ):
        if row.owner_user_id is None and not row.is_enabled:
            continue
        graph["mcp_ids"][row.server_id] = {
            "id": row.server_id,
            "name": row.display_name or row.server_id,
            "description": row.description or "",
            "dependencies": {},
        }
    for item in resources["mcp_servers"]:
        graph["mcp_ids"].setdefault(item["id"], {**item, "dependencies": {}})

    from core.capabilities.catalog.loader import DB_HIDDEN_SERVERS, DB_UMBRELLA_ID

    if DB_UMBRELLA_ID in graph["mcp_ids"]:
        graph["mcp_ids"][DB_UMBRELLA_ID]["dependencies"] = {
            "mcp_ids": [m for m in sorted(DB_HIDDEN_SERVERS) if m in graph["mcp_ids"]]
        }

    from core.capabilities.plugins.management import list_installed

    callable_plugins = {
        p["install_id"] for p in list_installed(db, user_id, include_global=True) if p["callable"]
    }
    for row in (
        db.query(InstalledPlugin)
        .filter(
            or_(InstalledPlugin.owner_user_id == user_id, InstalledPlugin.owner_user_id.is_(None))
        )
        .all()
    ):
        if row.install_id not in callable_plugins:
            continue
        parts = row.component_ids or {}
        graph["plugin_ids"][row.install_id] = {
            "id": row.install_id,
            "name": row.name,
            "description": row.description or "",
            "dependencies": {
                "skill_ids": _keys(parts.get("skills")),
                "mcp_ids": _keys(parts.get("mcp")),
                "agent_ids": _keys(parts.get("agents")),
                "plugin_ids": _keys(parts.get("plugins")),
            },
        }

    # Desktop projections use the same authorized account inventory as runtime.
    overlay = device_catalog.catalog_overlay(user_id)
    for field, entries in (("skill_ids", overlay["skills"]), ("mcp_ids", overlay["mcp"])):
        for entry in entries:
            graph[field].setdefault(entry["id"], {**entry, "dependencies": {}})
    from core.capabilities.identity_rules import KIND_PLUGIN

    device_plugins = device_catalog.installations(KIND_PLUGIN, user_id=user_id)
    for entry in device_catalog.plugin_entries(user_id):
        graph["plugin_ids"][entry["install_id"]] = {
            "id": entry["install_id"],
            "name": entry["name"],
            "description": entry["description"],
            "dependencies": {"skill_ids": _keys(entry["skills"]), "mcp_ids": _keys(entry["mcp"])},
        }
        # The picker uses the cloud installation identity; desktop activation
        # uses the account/profile-qualified installation identity. Keep both
        # in the authorized closure without duplicating picker entries.
        matching = [
            inst
            for inst in device_plugins
            if entry["install_id"] in (inst.install_id, inst.payload.get("cloud_install_id"))
        ]
        for inst in matching:
            if inst.install_id != entry["install_id"]:
                graph["plugin_ids"][entry["install_id"]]["dependencies"].setdefault(
                    "plugin_ids", []
                ).append(inst.install_id)
                graph["plugin_ids"][inst.install_id] = {
                    "id": inst.install_id,
                    "name": entry["name"],
                    "selectable": False,
                    "dependencies": {},
                }
        # Component inventory is authorized by the plugin projection. Runtime
        # preparation still verifies file readiness and account authorization.
        for field, parts in (("skill_ids", entry["skills"]), ("mcp_ids", entry["mcp"])):
            for ident in _keys(parts):
                graph[field].setdefault(ident, {"id": ident, "name": ident, "dependencies": {}})
    # Use the same authoritative ownership, suspension and readiness gate
    # as explicit chat invocation; listing a resource is not a permission grant.
    from core.capabilities.catalog.resolver import resolve_explicit_runtime_capabilities

    allowed_skills, allowed_mcps, _, _ = resolve_explicit_runtime_capabilities(
        db, user_id, skill_ids=list(graph["skill_ids"]), mcp_ids=list(graph["mcp_ids"])
    )
    graph["skill_ids"] = {k: v for k, v in graph["skill_ids"].items() if k in set(allowed_skills)}
    graph["mcp_ids"] = {k: v for k, v in graph["mcp_ids"].items() if k in set(allowed_mcps)}
    return graph
