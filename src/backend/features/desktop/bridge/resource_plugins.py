"""Interactive packages follow the conversation's device execution boundary."""

import json
from fastapi import HTTPException


def package_root(installed):
    revision = (installed.get("import_report") or {}).get("package_revision")
    if revision:
        from core.db.engine import SessionLocal
        from core.capabilities.plugins.packaging.runtime_assets import materialize

        with SessionLocal() as db:
            return materialize(db, revision)
    from core.capabilities.plugins.packaging.sources import resolve_plugin_dir

    return resolve_plugin_dir(installed["slug"])


def package_files(installed):
    ui = installed.get("ui_contributions") or {}
    modules = ui.get("contributes", {}).get("modules", [])
    if not any(module.get("resource") for module in modules):
        return {}, {}
    root = package_root(installed)
    if root is None:
        raise ValueError("plugin_package_missing")
    from extensions.mcp_servers._ports import PORTS

    declarations = json.loads((root / "mcp.json").read_text())["mcpServers"]
    native = {}
    for record in (installed.get("import_report") or {}).get("imported", []):
        if record.get("type") != "mcp":
            continue
        url = declarations.get(record["name"], {}).get("url", "")
        if url.startswith("builtin://") and url[10:] in PORTS:
            native[record["id"]] = url[10:]
    if not native:
        raise ValueError("interactive_native_connector_required")
    files = {}
    # Workers and Canvas assets are part of the same content-hashed revision.
    # Connector credentials and independent skill bundles are not exported here.
    for directory in {
        "web",
        *(module["resource"]["entry"] for module in modules if module.get("resource")),
    }:
        source = (root / directory).resolve()
        if not source.is_relative_to(root.resolve()) or not source.is_dir():
            raise ValueError("plugin_package_missing")
        for path in source.rglob("*"):
            if path.is_symlink():
                raise ValueError("package_symlink_not_allowed")
            if path.is_file() and "__pycache__" not in path.parts:
                files["package/" + path.relative_to(root).as_posix()] = path.read_text(
                    encoding="utf-8"
                )
    return files, native


def local_config(server, user_id, profile):
    from core.config.settings import settings

    if not settings.deploy.is_local:
        return None
    from features.desktop.capabilities import device_catalog, store
    from features.desktop.capabilities.plugins import load_manifest
    from features.desktop.capabilities.dependency import component_hash
    from features.desktop.capabilities.local_plugin_runtime import enabled_for
    from core.config.mcp_config import mcp_http_url as _mcp_http_url
    from extensions.mcp_servers._ports import PORTS

    for inst in device_catalog.installations("plugin", user_id=user_id):
        if inst.profile_id != profile or server["server_id"] not in inst.payload.get(
            "components", {}
        ).get("mcp", []):
            continue
        comp = store.get("plugin", profile, inst.key, inst.resolved_revision)
        if comp is None:
            raise HTTPException(409, "plugin_package_missing")
        definition = load_manifest(comp)
        modules = (
            (definition.get("ui_contributions") or {}).get("contributes", {}).get("modules", [])
        )
        if not any(module.get("resource") for module in modules):
            return None
        if not inst.ready or not enabled_for(inst, user_id):
            raise HTTPException(403, "plugin_not_ready")
        if component_hash(comp) != (inst.payload.get("resolved_content_hash") or inst.content_hash):
            raise HTTPException(409, "plugin_package_changed")
        audience = definition.get("native_servers", {}).get(server["server_id"])
        if audience not in PORTS:
            raise HTTPException(409, "interactive_plugin_sync_required")
        for module in modules:
            if module.get("resource"):
                source = comp.path / "package" / module["resource"]["entry"]
                if (
                    not source.resolve().is_relative_to((comp.path / "package").resolve())
                    or not source.is_dir()
                ):
                    raise HTTPException(409, "plugin_package_missing")
        return {
            "transport": "streamable_http",
            "url": _mcp_http_url(audience),
            "origin": "local_plugin",
            "execution_scope": "local",
            "owner_user_id": user_id,
            "source_plugin": inst.key,
            "plugin_install_id": inst.install_id,
            "execution_timeout": 180,
            "enable_tools": [tool["name"] for tool in server.get("tools", [])],
        }
    return None
