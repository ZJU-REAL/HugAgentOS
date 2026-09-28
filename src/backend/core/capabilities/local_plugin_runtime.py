"""Resolve plugin-owned local connectors directly from retained, authorized packages."""
import json
from . import registry, store
from .paths import LOCAL_PROFILE, capabilities_enabled


def enabled_for(inst, user_id):
    return bool(inst.enabled and inst.payload.get("user_enabled", {}).get(user_id, True))


def configs(user_id, enabled_only=True):
    if not user_id or not capabilities_enabled():
        return {}
    from .dependency import component_hash
    out = {}
    for inst in registry.list_installations(kind="plugin", profile_id=LOCAL_PROFILE):
        from .device_catalog import active
        owned = inst.payload.get("owner_user_id") == user_id or (inst.payload.get("shared_installation") and not active())
        if not owned or not inst.ready or (enabled_only and not enabled_for(inst, user_id)):
            continue
        comp = store.get("plugin", LOCAL_PROFILE, inst.key, inst.resolved_revision)
        if comp is None or component_hash(comp) != inst.content_hash:
            continue
        definition = json.loads((comp.path / "plugin.json").read_text(encoding="utf-8"))
        for sid, entry in definition.get("local_servers", {}).items():
            if enabled_only and not entry.get("enabled", True):
                continue
            config = json.loads(json.dumps(entry).replace("{plugin_root}", str(comp.path / "package").replace("\\", "\\\\")))
            config.update(source_plugin=inst.key, owner_user_id=user_id, origin="local_plugin", execution_scope="local", is_stable=False)
            executor = definition.get("manager_executor")
            if executor:
                from core.services.management_contract import tools, VERSION
                from core.services.desktop_capability_protocol import canonical_hash
                manager = executor.get("id")
                if executor.get("version") != VERSION:
                    raise ValueError("plugin executor upgrade required")
                from .skills import account_authorized_for
                cloud_available = account_authorized_for(user_id)
                schemas = tools(manager, "local")
                if not cloud_available:
                    schemas = [t for t in schemas if t["name"] != "upload_skill_to_cloud"]
                config.update(transport="streamable_http", url="http://localhost/plugin-executor",
                    schema_source="plugin_manifest", manifest_tools=schemas, gateway_invoke_url="",
                    schema_hash=canonical_hash(schemas), gateway_plugin=manager, manager_cloud_available=cloud_available,
                    headers={"X-Current-User-Id": user_id})
            out[sid] = config
    return out


def authorizes_manager(user_id, server_id, manager):
    cfg = configs(user_id).get(server_id)
    return bool(cfg and cfg.get("gateway_plugin") == manager)
