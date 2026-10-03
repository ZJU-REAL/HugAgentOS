"""Plugin catalog views over authorized device installations."""
from typing import Any, Dict, List, Optional
from . import device_catalog as catalog
from .local_plugin_runtime import enabled_for
from .paths import KIND_PLUGIN, KIND_SKILL, capabilities_enabled

# ── 插件 ──────────────────────────────────────────────────────────────


def plugin_entries(user_id=None) -> List[Dict[str, Any]]:
    """云端账号的插件投影成 ``/v1/plugins/installed`` 的条目结构。"""
    if not capabilities_enabled():
        return []
    from .management_context import management_context
    from .readiness import file_readiness
    context = management_context(user_id)
    entries: List[Dict[str, Any]] = []
    for inst in catalog._installations(KIND_PLUGIN, user_id=user_id):
        components = dict(inst.payload.get("components") or {})
        readiness = file_readiness(inst, context)
        entries.append(
            {
                "install_id": str(inst.payload.get("cloud_install_id") or inst.install_id),
                "slug": inst.key,
                "name": inst.payload.get("presentation", {}).get("display_name") or inst.display_name or inst.key,
                "version": inst.version or "",
                "description": inst.description or "",
                "category": str(inst.payload.get("presentation", {}).get("category", inst.payload.get("category")) or ""),
                "icon": inst.payload.get("presentation", {}).get("icon"),
                "source": inst.source,
                "enabled": enabled_for(inst, user_id),
                # 本机可用性：文件已就绪才谈得上调用，与用户的开关无关。
                "callable": bool(inst.ready and readiness["ready"]),
                "readiness": readiness,
                "is_global": bool(inst.payload.get("shared_installation")),
                "skills": list(components.get("skills") or []),
                "mcp": list(components.get("mcp") or []),
                "tools": [],
                "import_report": inst.payload.get("import_report", {}),
                "created_at": inst.created_at.isoformat() if inst.created_at else None,
            }
        )
    return entries


def plugin_ui_contributions(user_id=None) -> List[Dict[str, Any]]:
    """云端投影插件的界面贡献（只给本机启用着的那些）。

    界面贡献必须和启停同源：启停写在本机登记表，所以这份声明也从本机存的清单读。
    回头去问云端的话，用户在本机关掉的插件、面板还会留在界面上。
    """
    if not capabilities_enabled():
        return []
    from core.plugins.ui.contract import public_contributions

    from . import plugins as caps_plugins, store

    out: List[Dict[str, Any]] = []
    for inst in catalog._installations(KIND_PLUGIN, user_id=user_id):
        if not (enabled_for(inst, user_id) and inst.ready):
            continue
        comp = store.get(KIND_PLUGIN, inst.profile_id, inst.key, inst.resolved_revision)
        if comp is None:
            continue
        try:
            manifest = caps_plugins.load_manifest(comp)
        except (OSError, ValueError):
            # 清单读不出来只影响这一个插件的界面，不该让整页拿不到贡献。
            continue
        public = public_contributions(manifest.get("ui_contributions"), slug=inst.key)
        if public.get("contributes"):
            out.append(public)
    return out


def _projected_plugin(install_id_or_slug: str, user_id=None):
    """按云端 install_id、slug 或本机 install_id 定位一条投影插件。"""
    for inst in catalog._installations(KIND_PLUGIN, user_id=user_id):
        cloud_id = str(inst.payload.get("cloud_install_id") or "")
        if install_id_or_slug in (inst.key, cloud_id, inst.install_id, *inst.payload.get("legacy_ids", [])):
            return inst
    return None


def _component_skill(profile: str, skill_id: str) -> Optional[Dict[str, Any]]:
    from core.config.catalog_details import skill_body_from_raw

    from . import registry

    inst = registry.get(registry.install_id(KIND_SKILL, profile, skill_id))
    if inst is None or inst.state == "removed":
        return None
    body = catalog._skill_body(inst)
    return {
        "skill_id": skill_id,
        "name": inst.display_name or skill_id,
        "description": inst.description or "",
        "version": inst.version or "",
        "tags": [],
        "enabled": bool(inst.enabled),
        "instructions": skill_body_from_raw(body[0]) if body else "",
        "files": list(body[1]) if body else [],
        "has_secrets": False,
    }


def _component_connector(server_id: str, known: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    entry = known.get(server_id, {})
    return {
        "server_id": server_id,
        "name": str(entry.get("display_name") or server_id),
        "description": str(entry.get("description") or ""),
        # 云端下发的连接器一律经云端网关调用，本机不起进程。
        "transport": entry.get("transport", "cloud_gateway"),
        "url": None,
        "enabled": bool(entry.get("enabled", True)),
        "needs_runtime": entry.get("transport") == "stdio",
        "tools": list(entry.get("tools") or []),
    }


def plugin_detail(install_id_or_slug: str, user_id=None) -> Optional[Dict[str, Any]]:
    """云端投影插件的详情：它的技能与连接器都从登记表取，界面才不会是个空壳。"""
    if not capabilities_enabled():
        return None
    inst = _projected_plugin(install_id_or_slug, user_id)
    if inst is None:
        return None
    profile = inst.profile_id
    components = dict(inst.payload.get("components") or {})
    skills = [
        item
        for item in (_component_skill(profile, str(sid)) for sid in components.get("skills") or [])
        if item is not None
    ]
    known = {c["server_id"]: c for c in catalog._managed_connectors()}
    if profile == "local":
        from .local_plugin_runtime import configs
        known.update(configs(user_id or inst.payload.get("owner_user_id"), enabled_only=False))
    return {
        "install_id": str(inst.payload.get("cloud_install_id") or inst.install_id),
        "slug": inst.key,
        "name": inst.payload.get("presentation", {}).get("display_name") or inst.display_name or inst.key,
        "is_global": bool(inst.payload.get("shared_installation")),
        "version": inst.version or "",
        "description": inst.description or "",
        "category": str(inst.payload.get("presentation", {}).get("category", inst.payload.get("category")) or ""),
        "icon": inst.payload.get("presentation", {}).get("icon"),
        "source": inst.source,
        "import_report": inst.payload.get("import_report", {}),
        "skills": skills,
        "mcp": [_component_connector(str(mid), known) for mid in components.get("mcp") or []],
        "admin_config": None,
        "connection": None,
    }
