"""Plugin catalog views over authorized device installations."""
from typing import Any, Dict, List, Optional
from features.desktop.capabilities.local_plugin_runtime import enabled_for
from core.capabilities.identity_rules import KIND_PLUGIN, KIND_SKILL
from features.desktop.capabilities.paths import capabilities_enabled

# ── 插件 ──────────────────────────────────────────────────────────────


def plugin_entries(user_id=None) -> List[Dict[str, Any]]:
    """云端账号的插件投影成 ``/v1/plugins/installed`` 的条目结构。"""
    if not capabilities_enabled():
        return []
    from features.desktop.capabilities.management_context import management_context
    from features.desktop.capabilities.readiness import file_readiness
    context = management_context(user_id)
    entries: List[Dict[str, Any]] = []
    for inst in catalog.installations(KIND_PLUGIN, user_id=user_id):
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
    from core.harness.registry.package_views import public_package_view

    from features.desktop.capabilities import plugins as caps_plugins, store

    out: List[Dict[str, Any]] = []
    for inst in catalog.installations(KIND_PLUGIN, user_id=user_id):
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
        public = public_package_view(manifest.get("ui_contributions"), slug=inst.key)
        if public.get("contributes"):
            out.append(public)
    return out


def _projected_plugin(install_id_or_slug: str, user_id=None):
    """按云端 install_id、slug 或本机 install_id 定位一条投影插件。"""
    for inst in catalog.installations(KIND_PLUGIN, user_id=user_id):
        cloud_id = str(inst.payload.get("cloud_install_id") or "")
        if install_id_or_slug in (inst.key, cloud_id, inst.install_id, *inst.payload.get("legacy_ids", [])):
            return inst
    return None


def _component_skill(profile: str, skill_id: str) -> Optional[Dict[str, Any]]:
    from core.capabilities.catalog.details import skill_body_from_raw

    from features.desktop.capabilities import registry

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
    from core.capabilities.plugins.connections import manifest_connection
    from features.desktop.capabilities import plugins, store

    comp = store.get("plugin", inst.profile_id, inst.key, inst.resolved_revision)
    manifest = plugins.load_manifest(comp) if comp else {}
    profile = inst.profile_id
    components = dict(inst.payload.get("components") or {})
    skills = [
        item
        for item in (_component_skill(profile, str(sid)) for sid in components.get("skills") or [])
        if item is not None
    ]
    known = {c["server_id"]: c for c in catalog._managed_connectors()}
    if profile == "local":
        from features.desktop.capabilities.local_plugin_runtime import configs
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
        "connection": manifest_connection(manifest),
    }


def resolve_resource_plugin(slug, user_id, *, install_id=None):
    """Resolve the exact device installation for a resource launch."""
    from core.capabilities.plugins.resources.installation import Installation
    from fastapi import HTTPException
    from features.desktop.capabilities import store
    from features.desktop.capabilities.device_plugin_catalog import _projected_plugin
    from features.desktop.capabilities.local_plugin_runtime import enabled_for
    from features.desktop.capabilities.plugins import load_manifest

    candidate = _projected_plugin(install_id or slug, user_id=user_id)
    if candidate is not None:
        if candidate.key != slug or not candidate.ready or not enabled_for(candidate, user_id):
            raise HTTPException(403, "plugin_not_ready")
        component = store.get(
            "plugin", candidate.profile_id, candidate.key, candidate.resolved_revision
        )
        if component is None:
            raise HTTPException(409, "plugin_package_missing")
        manifest = load_manifest(component)
        package = component.path / "package"
        if not package.is_dir():
            raise HTTPException(409, "plugin_package_missing")
        return Installation(
            candidate.install_id,
            candidate.resolved_revision,
            slug,
            manifest.get("ui_contributions") or {},
            package,
        )
    return None


# device_catalog re-exports this module's functions at import time, so it is
# bound last: by then those names exist whichever module was imported first.
from features.desktop.capabilities import device_catalog as catalog  # noqa: E402
