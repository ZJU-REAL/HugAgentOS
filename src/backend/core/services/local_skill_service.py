"""Device-owned skill lifecycle over the capability index and immutable versions."""
from pathlib import Path
from sqlalchemy import update as sql_update
from core.capabilities import registry, store, skills
from core.capabilities.paths import LOCAL_PROFILE, require_root, revision_for_hash
from core.capabilities.ref import local_ref
from core.db.models import DeviceCapabilityInstallation
from core.services.management_package import snapshot, serialized


def _owned(user_id, install_id, db=None):
    if not user_id:
        raise PermissionError("authentication required")
    inst = registry.get(install_id, db=db)
    if (not inst or inst.kind != "skill" or inst.profile_id != LOCAL_PROFILE
            or inst.payload.get("owner_user_id") != user_id or inst.state == "removed"):
        raise PermissionError("skill does not exist or is not owned by current user")
    if inst.source_plugin:
        raise ValueError("plugin component must be managed through its plugin")
    return inst


def _result(inst):
    from core.capabilities.readiness import file_readiness
    from core.capabilities.management_context import management_context
    comp = store.get("skill", LOCAL_PROFILE, inst.key, inst.resolved_revision)
    return {"ok": True, "install_id": inst.install_id, "skill_id": inst.key,
            "name": inst.display_name, "revision": inst.resolved_revision,
            "source": "local", "enabled": inst.enabled, "path": str(comp.path) if comp else None,
            "readiness": file_readiness(inst, management_context(inst.payload.get("owner_user_id")))}


def get(user_id, install_id):
    return _result(_owned(user_id, install_id))


def list_skills(user_id):
    if not user_id:
        raise PermissionError("authentication required")
    return [_result(x) for x in registry.list_installations(kind="skill", profile_id=LOCAL_PROFILE)
            if x.payload.get("owner_user_id") == user_id and not x.source_plugin]


def _prepare(user_id, root, key=None):
    from core.agent_skills.registry import _load_skill_metadata_from_str, _split_frontmatter
    from core.services.marketplace_service import compute_install_id, _rewrite_frontmatter_name
    from core.services.desktop_capability_protocol import skill_content_hash
    from core.agent_skills.binary_files import encode_upload
    md = (root / "SKILL.md").read_text(encoding="utf-8")
    fm, _ = _split_frontmatter(md)
    original = str(fm.get("name") or "")
    _load_skill_metadata_from_str(md, original)
    key = key or compute_install_id(original, user_id)
    md = _rewrite_frontmatter_name(md, key)
    meta = _load_skill_metadata_from_str(md, key)
    from core.capabilities.archive import iter_files
    files = {name: file.read_bytes() for name, file in iter_files(root)}
    files["SKILL.md"] = md
    extras = {k: encode_upload(k, v) for k, v in files.items() if k != "SKILL.md"}
    digest = skill_content_hash(md, extras)
    revision = revision_for_hash(digest)
    comp = store.write_from_files("skill", LOCAL_PROFILE, key, revision, files)
    return key, meta, digest, comp, original


def _invalidate():
    from core.config.catalog_runtime import invalidate_runtime_catalog_cache
    from core.config.catalog_resolver import invalidate_capability_cache
    from prompts.prompt_runtime import invalidate_prompt_cache
    invalidate_capability_cache()
    invalidate_prompt_cache()
    skills.bump_view_generation()
    invalidate_runtime_catalog_cache()
    from core.agent_skills.loader import get_skill_loader
    get_skill_loader(reset=True)


@serialized
def install(user_id, source_path):
    if not user_id:
        raise PermissionError("authentication required")
    require_root()
    with snapshot(source_path) as root:
        key, meta, digest, comp, original = _prepare(user_id, root)
    iid = registry.install_id("skill", LOCAL_PROFILE, key)
    with registry._session() as db:
        existing = registry.get(iid, db=db)
        if existing and existing.state != "removed":
            if not existing.source_plugin and existing.payload.get("owner_user_id") == user_id and existing.content_hash == digest:
                return _result(existing)
            raise ValueError("name_conflict: use update_skill with install_id and expected_revision")
        registry.upsert(profile_id=LOCAL_PROFILE, ref=local_ref("skill", key),
            display_name=original, description=meta.description, version=meta.version,
            content_hash=digest, source="local", enabled=True,
            payload={"owner_user_id": user_id, "managed_by": "skill-manager", "from_db": False}, db=db)
        registry.set_state(iid, "ready", resolved_revision=comp.revision, db=db)
    _invalidate()
    return get(user_id, iid)


@serialized
def update(user_id, install_id, source_path, expected_revision):
    inst = _owned(user_id, install_id)
    if not expected_revision or inst.resolved_revision != expected_revision:
        raise ValueError("revision_conflict")
    with snapshot(source_path) as root:
        key, meta, digest, comp, original = _prepare(user_id, root, inst.key)
    with registry._session() as db:
        current = _owned(user_id, install_id, db)
        changed = db.execute(sql_update(DeviceCapabilityInstallation).where(
            DeviceCapabilityInstallation.install_id == install_id,
            DeviceCapabilityInstallation.resolved_revision == expected_revision,
            DeviceCapabilityInstallation.state != "removed",
        ).values(resolved_revision=comp.revision, content_hash=digest,
                 description=meta.description, version=meta.version,
                 generation=DeviceCapabilityInstallation.generation + 1,
                 payload={**current.payload, "resolved_content_hash": digest}))
        if changed.rowcount != 1:
            raise ValueError("revision_conflict")
    _invalidate()
    return get(user_id, install_id)


@serialized
def uninstall(user_id, install_id, expected_revision):
    with registry._session() as db:
        inst = _owned(user_id, install_id, db)
        if not expected_revision or inst.resolved_revision != expected_revision:
            raise ValueError("revision_conflict")
        changed = db.execute(sql_update(DeviceCapabilityInstallation).where(
            DeviceCapabilityInstallation.install_id == install_id,
            DeviceCapabilityInstallation.resolved_revision == expected_revision,
            DeviceCapabilityInstallation.state != "removed",
        ).values(state="removed", enabled=False, generation=DeviceCapabilityInstallation.generation + 1))
        if changed.rowcount != 1:
            raise ValueError("revision_conflict")
    _invalidate()
    return {"ok": True, "install_id": install_id, "source": "local", "action": "uninstalled"}


@serialized
def set_presentation(user_id, install_id, values, expected_revision=None):
    with registry._session() as db:
        inst = _owned(user_id, install_id, db)
        if expected_revision and inst.resolved_revision != expected_revision:
            raise ValueError("revision_conflict")
        registry.set_state(install_id, inst.state,
            payload_update={"presentation": {**inst.payload.get("presentation", {}), **values}}, db=db)
    _invalidate()
