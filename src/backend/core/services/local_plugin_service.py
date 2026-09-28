"""Device plugin lifecycle: immutable packages, transactional ownership and activation."""
import json
import tempfile
from pathlib import Path
from sqlalchemy import update as sql_update
from core.capabilities import registry, store, archive
from core.capabilities.paths import LOCAL_PROFILE, require_root, revision_for_hash
from core.capabilities.ref import local_ref
from core.capabilities.plugins import component_install_ids
from core.db.models import DeviceCapabilityInstallation
from core.services.management_package import snapshot, serialized
from core.services import local_skill_service as skill_service


def _owned(user_id, install_id, db=None):
    inst = registry.get(install_id, db=db)
    if (not user_id or not inst or inst.kind != "plugin" or inst.profile_id != LOCAL_PROFILE
            or inst.state == "removed" or inst.payload.get("owner_user_id") != user_id):
        raise PermissionError("plugin does not exist or is not owned by current user")
    return inst


def get(user_id, install_id):
    inst = _owned(user_id, install_id)
    comp = store.get("plugin", LOCAL_PROFILE, inst.key, inst.resolved_revision)
    return {"ok": True, "install_id": inst.install_id, "slug": inst.key,
            "name": inst.display_name, "revision": inst.resolved_revision, "source": "local",
            "version": inst.version, "description": inst.description, "enabled": inst.enabled,
            "path": str(comp.path) if comp else None,
            "components": inst.payload.get("components", {}),
            "import_report": inst.payload.get("import_report", {})}


def list_plugins(user_id):
    if not user_id:
        raise PermissionError("authentication required")
    return [get(user_id, x.install_id) for x in registry.list_installations(kind="plugin", profile_id=LOCAL_PROFILE)
            if x.payload.get("owner_user_id") == user_id]


def _prepare(user_id, root, key=None, *, skill_ids=None, server_ids=None):
    from core.services.plugin_importer import normalize_plugin_dir, detect_manifest
    from core.services.marketplace_service import compute_install_id
    from core.services.desktop_capability_protocol import entity_content_hash
    from core.capabilities.mcp_json import _validate_local_server
    np = normalize_plugin_dir(root)
    manifest = json.loads(detect_manifest(root)[1].read_text(encoding="utf-8"))
    executor = (manifest.get("extensions", {}).get("org.hugagent", {}).get("local_execution"))
    if executor:
        from core.services.management_contract import MANAGERS, VERSION
        if executor.get("id") != np.slug or np.slug not in MANAGERS or executor.get("version") != VERSION:
            raise ValueError("unsupported plugin executor; upgrade required")
    key = key or compute_install_id(np.slug, user_id)
    children, servers = [], {}
    skill_ids = {child.name: (skill_ids or {}).get(child.name) or compute_install_id(child.name, key) for child in np.skills}
    server_ids = {mc.name: (server_ids or {}).get(mc.name) or compute_install_id(mc.name, key) for mc in np.mcp}
    for mcp in np.mcp:
        if mcp.headers:
            raise ValueError("package credentials must be configured outside plugin contents")
        sid = server_ids[mcp.name]
        raw = {"transport": mcp.transport, "command": mcp.command, "args": mcp.args,
               "url": mcp.url, "env": mcp.env_vars, "cwd": mcp.cwd,
               "enabled": mcp.name in np.default_enabled.get("mcp", [])}
        entry = _validate_local_server(sid, raw)
        # Keep paths relative to the retained package, never to the import staging directory.
        entry = json.loads(json.dumps(entry).replace("/workspace/plugins/" + mcp.name, "{plugin_root}"))
        servers[sid] = {**entry, "display_name": mcp.display_name, "description": mcp.description,
                        "tools": mcp.tools}
    components = {"skills": list(skill_ids.values()), "mcp": list(servers)}
    definition = {"slug": key, "package_slug": np.slug, "name": np.name, "version": np.version,
                  "description": np.description, "category": np.category,
                  "components": components, "component_identity_map": {"skills": skill_ids, "mcp": server_ids}, "local_servers": servers, "manager_executor": executor, "ui_contributions": np.ui,
                  "import_report": {"dropped": np.dropped}}
    files = {"package/" + n: p.read_bytes() for n, p in archive.iter_files(root)}
    # entity_content_hash represents binary assets using the existing wire encoding.
    from core.agent_skills.binary_files import encode_upload
    files["plugin.json"] = json.dumps(definition, ensure_ascii=False, sort_keys=True)
    digest = entity_content_hash({n: encode_upload(n, v) if isinstance(v, bytes) else v for n, v in files.items()})
    comp = store.write_from_files("plugin", LOCAL_PROFILE, key, revision_for_hash(digest), files)
    with tempfile.TemporaryDirectory(prefix="plugin-skills-") as tmp:
        for child in np.skills:
            child_key = skill_ids[child.name]
            directory = Path(tmp) / child_key
            from core.services.plugin_skill_transform import files as component_files
            from core.capabilities.skills import user_view_dir, device_view_dir
            archive.write_files(directory, component_files(child, skill_ids, server_ids, (user_view_dir(user_id) or device_view_dir()) / child_key, comp.path / "package"))
            children.append(skill_service._prepare(user_id, directory, child_key))
    return np, key, definition, digest, comp, children


def _publish(user_id, prepared, db, *, legacy_source=None):
    np, key, definition, digest, comp, children = prepared
    iid = registry.install_id("plugin", LOCAL_PROFILE, key)
    old_edges = registry.components_of(iid, db=db)
    new_edges = component_install_ids(LOCAL_PROFILE, definition)
    for child_key, meta, child_hash, child_comp, original in children:
        cid = registry.install_id("skill", LOCAL_PROFILE, child_key)
        previous = registry.get(cid, db=db)
        if previous and (previous.source_plugin not in (key, legacy_source) or previous.payload.get("owner_user_id") != user_id):
            raise ValueError("component ownership conflict")
        registry.upsert(profile_id=LOCAL_PROFILE, ref=local_ref("skill", child_key),
                        display_name=original, description=meta.description, version=meta.version,
                        content_hash=child_hash, source="local", source_plugin=key,
                        enabled=previous.enabled if previous and previous.state != "removed" else original in np.default_enabled.get("skills", []),
                        payload={"owner_user_id": user_id, "managed_by": "plugin-manager", "from_db": False, "shared_installation": user_id is None}, db=db)
        registry.set_state(cid, "ready", resolved_revision=child_comp.revision, db=db)
    for cid in old_edges.keys() - new_edges.keys():
        registry.mark_removed(cid, db=db)
    previous = registry.get(iid, db=db)
    registry.upsert(profile_id=LOCAL_PROFILE, ref=local_ref("plugin", key),
                    display_name=np.name, description=np.description, version=np.version,
                    content_hash=digest, source="local", enabled=previous.enabled if previous and previous.state != "removed" else True,
                    payload={"owner_user_id": user_id, "managed_by": "plugin-manager", "from_db": False, "shared_installation": user_id is None,
                             "components": definition["components"], "component_identity_map": definition["component_identity_map"], "category": np.category,
                             "import_report": definition["import_report"]}, db=db)
    registry.set_components(iid, new_edges, db=db)
    registry.set_state(iid, "ready", resolved_revision=comp.revision, db=db)
    return iid


@serialized
def install(user_id, source_path):
    if not user_id:
        raise PermissionError("authentication required")
    require_root()
    with snapshot(source_path) as root:
        prepared = _prepare(user_id, root)
    iid = registry.install_id("plugin", LOCAL_PROFILE, prepared[1])
    with registry._session() as db:
        existing = registry.get(iid, db=db)
        if existing and existing.state != "removed":
            if existing.payload.get("owner_user_id") != user_id or existing.content_hash != prepared[3]:
                raise ValueError("name_conflict: use update_plugin with install_id and expected_revision")
        else:
            _publish(user_id, prepared, db)
    skill_service._invalidate()
    return get(user_id, iid)


def _claim(db, inst, expected_revision):
    if not expected_revision or inst.resolved_revision != expected_revision:
        raise ValueError("revision_conflict")
    result = db.execute(sql_update(DeviceCapabilityInstallation).where(
        DeviceCapabilityInstallation.install_id == inst.install_id,
        DeviceCapabilityInstallation.resolved_revision == expected_revision,
        DeviceCapabilityInstallation.state != "removed",
    ).values(generation=DeviceCapabilityInstallation.generation + 1))
    if result.rowcount != 1:
        raise ValueError("revision_conflict")


@serialized
def update(user_id, install_id, source_path, expected_revision):
    inst = _owned(user_id, install_id)
    with snapshot(source_path) as root:
        ids = inst.payload.get("component_identity_map", {})
        prepared = _prepare(user_id, root, inst.key, skill_ids=ids.get("skills"), server_ids=ids.get("mcp"))
    with registry._session() as db:
        current = _owned(user_id, install_id, db)
        _claim(db, current, expected_revision)
        _publish(user_id, prepared, db)
    skill_service._invalidate()
    return get(user_id, install_id)


@serialized
def uninstall(user_id, install_id, expected_revision):
    with registry._session() as db:
        inst = _owned(user_id, install_id, db)
        _claim(db, inst, expected_revision)
        for cid in registry.components_of(install_id, db=db):
            registry.mark_removed(cid, db=db)
        registry.mark_removed(install_id, db=db)
    skill_service._invalidate()
    return {"ok": True, "install_id": install_id, "source": "local", "action": "uninstalled"}


@serialized
def set_presentation(user_id, install_id, values):
    from core.services.plugin_service import _validate_icon
    with registry._session() as db:
        inst = _owned(user_id, install_id, db)
        presentation = dict(inst.payload.get("presentation") or {})
        for key in ("display_name", "category", "icon"):
            if key in values:
                presentation[key] = _validate_icon(values[key]) if key == "icon" else str(values[key])[:255]
        registry.set_state(install_id, inst.state, payload_update={"presentation": presentation}, db=db)
    skill_service._invalidate()
    return {"install_id": install_id, **presentation}


@serialized
def set_enabled_for_user(user_id, install_id, enabled):
    with registry._session() as db:
        inst = registry.get(install_id, db=db)
        if not user_id or not inst or inst.state == "removed" or inst.profile_id != "local" or inst.kind != "plugin":
            raise PermissionError("plugin not found")
        if inst.payload.get("shared_installation"):
            overrides = {**inst.payload.get("user_enabled", {}), user_id: bool(enabled)}
            registry.set_state(install_id, inst.state, payload_update={"user_enabled": overrides}, db=db)
        else:
            _owned(user_id, install_id, db)
            registry.set_enabled(install_id, enabled, db=db)
    skill_service._invalidate()
