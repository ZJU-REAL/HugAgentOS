"""Shared connector resolution and dependency context for management surfaces."""
from typing import Any, Dict
from . import registry, skills


def _authorized_profile(user_id):
    return skills.current_account_profile() if skills.account_authorized_for(user_id) else None


def management_context(user_id: str):
    from core.capabilities.readiness import context_for_user

    connectors = connectors_view(user_id)
    available = {
        entry["server_id"]
        for entry in connectors["items"]
        if entry["usable"] and entry["resolution"]["outcome"] == "chosen"
    }
    return context_for_user(user_id, available_mcp=available)


def connectors_view(user_id: str) -> Dict[str, Any]:
    """Connector bindings as the resolver last decided them for this device."""
    from core.capabilities import connectors, skills, mcp_json
    from core.services.mcp_service import McpServerConfigService

    svc = McpServerConfigService.get_instance()
    all_cfgs = {
        sid: cfg
        for sid, cfg in svc.get_all_servers(enabled_only=False).items()
        if cfg.get("owner_user_id") in (None, user_id)
    }
    all_cfgs.update(svc.get_owned_servers(user_id, enabled_only=False))
    enabled_ids = set(svc.get_all_servers(enabled_only=True)) | set(svc.get_owned_servers(user_id))
    from core.services.desktop_cloud_bridge import _mcp_json_local_declarations

    candidates = connectors.db_candidates(all_cfgs, enabled_ids) + connectors.json_candidates(
        _mcp_json_local_declarations()
    )
    # The runtime context excludes disabled bindings; the management view must
    # keep them visible so users can turn them back on.
    from core.services.desktop_cloud_bridge import get_cached_manifest

    profile = _authorized_profile(user_id)
    manifest = get_cached_manifest() if profile else None
    if manifest:
        candidates += connectors.cloud_candidates(
            profile, manifest.get("servers") or [], mcp_json.managed_enabled(profile)
        )
    res = connectors.resolve_bindings(
        candidates, user_id=user_id
    )
    chosen = {c.install_id for c in res.chosen.values()}
    shadowed = {c.install_id for cs in res.shadowed.values() for c in cs}
    conflicted = {c.install_id for cs in res.conflicts.values() for c in cs}
    items = []
    for c in candidates:
        entry = c.to_dict()
        entry["server_id"] = connectors.server_id_of(c)
        entry["enabled"] = c.state != "disabled"
        entry["transport"] = (
            "cloud_gateway"
            if c.source == "cloud"
            else (
                _mcp_json_local_declarations().get(entry["server_id"], {}).get("transport")
                if c.profile == connectors.MCP_JSON_PROFILE
                else all_cfgs.get(entry["server_id"], {}).get("transport")
            )
        )
        outcome = (
            "chosen"
            if c.install_id in chosen
            else (
                "conflict"
                if c.install_id in conflicted
                else "shadowed" if c.install_id in shadowed else "unusable"
            )
        )
        entry["resolution"] = {"outcome": outcome, "reason": res.reasons.get(c.runtime_name)}
        from core.capabilities.readiness import apply_to_item, connector_readiness

        config = (
            _mcp_json_local_declarations().get(entry["server_id"], {})
            if c.profile == connectors.MCP_JSON_PROFILE
            else all_cfgs.get(entry["server_id"], {})
        )
        apply_to_item(entry, connector_readiness(c, config))
        items.append(entry)
    return {
        "kind": "mcp",
        "profile_id": _authorized_profile(user_id),
        "items": items,
        "conflicts": {n: [c.install_id for c in cs] for n, cs in res.conflicts.items()},
        "preferences": registry.preferences("mcp", user_id=user_id),
    }
