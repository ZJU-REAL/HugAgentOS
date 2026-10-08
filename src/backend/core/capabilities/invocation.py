"""Explicit desktop selections use the same authorized sources as runtime views."""

from . import connectors, plugins, registry, skills
from .errors import NameConflict


def cloud_plugin_selection(ident, *, user_id):
    from .local_plugin_runtime import configs, enabled_for
    from .device_catalog import active
    local = [row for row in registry.list_installations(kind="plugin", profile_id="local")
             if (row.payload.get("owner_user_id") == user_id or (row.payload.get("shared_installation") and not active()))
             and ident in (row.key, row.install_id, *row.payload.get("legacy_ids", []))]
    if local:
        row = local[0]
        components = row.payload.get("components", {}) if enabled_for(row, user_id) and row.ready else {}
        available = configs(user_id)
        return {"install_id": row.install_id, "name": row.display_name,
                "skills": [sid for sid in components.get("skills", [])
                           if (child := registry.get(registry.install_id("skill", "local", sid))) and child.enabled and child.ready],
                "mcp": [sid for sid in components.get("mcp", []) if sid in available]}
    if not skills.account_authorized_for(user_id):
        return None
    profile = skills.current_account_profile()
    matches = [
        row
        for row in registry.list_installations(kind="plugin", profile_id=profile)
        if ident in (row.install_id, row.key, row.payload.get("cloud_install_id"))
    ]
    if len(matches) > 1:
        raise NameConflict("choose a plugin source")
    if not matches:
        return None
    row = matches[0]
    if row.enabled and not row.ready:
        # 用户在对话里直接选了一个尚未下载的云端插件：按需准备定义与组件。
        from .preparation import ensure_cloud_ready

        from .errors import CapabilityError

        try:
            ensure_cloud_ready(user_id, install_ids=[row.install_id])
        except (CapabilityError, OSError, ValueError):
            pass
        row = registry.get(row.install_id) or row
    if not row.enabled or not row.ready:
        # 选中的插件还没就绪：如实返回一个没有组件的它，让调用方照常说明，
        # 而不是把整轮打断。
        return {
            "install_id": row.install_id,
            "name": row.display_name or row.key,
            "skills": [],
            "mcp": [],
        }
    from .preparation import ensure_cloud_ready as _ensure_components

    from .errors import CapabilityError

    try:
        _ensure_components(user_id, install_ids=[row.install_id])
        skill_ids, mcp_ids = plugins.cloud_binding_ids([row.install_id], user_id=user_id)
    except (CapabilityError, OSError, ValueError):
        skill_ids, mcp_ids = [], []
    return {
        "install_id": row.install_id,
        "name": row.display_name or row.key,
        "skills": skill_ids,
        "mcp": mcp_ids,
    }


def resolve_explicit_ids(user_id, allowed_skill_ids, allowed_mcp_ids):
    """Apply device source selection without reviving disabled/conflicting rows.

    Catalog-only local entries retain existing explicit-invocation semantics.
    For names present in the desktop store, its current resolver is authoritative.
    This function never installs a component or imports a cloud credential.
    """
    from core.services import desktop_cloud_bridge as bridge

    skill_candidates = skills.candidates(user_id)
    skill_resolution = skills.resolve_for_user(user_id)
    known_skills = {candidate.runtime_name for candidate in skill_candidates}
    allowed_skills = set(allowed_skill_ids) - known_skills
    allowed_skills.update(skill_resolution.chosen)

    context = bridge._bridge_context() if skills.account_authorized_for(user_id) else None
    local_ids = bridge._local_server_ids()
    local_json = bridge._mcp_json_local_declarations()
    candidates = connectors.db_candidates(local_ids, set(allowed_mcp_ids), user_id=user_id)
    candidates.extend(connectors.json_candidates(local_json))
    if context:
        candidates.extend(connectors.cloud_candidates(context["profile"], context["servers"], {}))
    resolution = connectors.resolve_bindings(candidates, user_id=user_id)
    known_mcp = {connectors.server_id_of(candidate) for candidate in candidates}
    allowed_mcps = set(allowed_mcp_ids) - known_mcp
    allowed_mcps.update(
        connectors.server_id_of(candidate) for candidate in resolution.chosen.values()
    )
    return allowed_skills, allowed_mcps
