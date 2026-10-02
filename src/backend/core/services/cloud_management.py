"""Cloud adapters for the plugin-owned management contract."""

from core.plugins.packaging import sources as plugin_sources
from contextlib import contextmanager
import hashlib
import json
import tempfile
import uuid
from sqlalchemy import text
from pathlib import Path
from core.capabilities import archive
from core.agent_skills.binary_files import encode_upload
from core.services.management_package import snapshot
from core.db.engine import SessionLocal
from core.db.models import AdminSkill, AdminMcpServer, InstalledPlugin, Artifact, ContentBlock
from core.services import capability_workcopies as copies


@contextmanager
def artifact_package(user_id, source):
    if not user_id or source.get("kind") != "artifact" or set(source) != {"kind", "artifact_id"}:
        raise ValueError("cloud installation requires source.kind=artifact and artifact_id")
    with SessionLocal() as db:
        artifact = db.get(Artifact, source["artifact_id"])
        if not artifact or artifact.user_id != user_id or artifact.deleted_at is not None:
            raise PermissionError("artifact does not exist or is not owned by current user")
        if artifact.size_bytes > archive.MAX_TOTAL_BYTES:
            raise ValueError("artifact package too large")
        key = artifact.storage_key
    from core.storage import get_storage

    data = get_storage().download_bytes(key)
    if len(data) > archive.MAX_TOTAL_BYTES:
        raise ValueError("artifact package too large")
    with tempfile.TemporaryDirectory(prefix="cloud-manager-") as tmp:
        package = Path(tmp) / "package.archive"
        package.write_bytes(data)
        with snapshot(str(package)) as root:
            yield root


def _write_lock(db):
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))


def _revision(db, kind, row, snap):
    if kind == "skill":
        return snap["revision"]
    children = {}
    for model, identity in ((AdminSkill, "skill_id"), (AdminMcpServer, "server_id")):
        rows = (
            db.query(model)
            .filter(model.source_plugin == row.slug, model.owner_user_id == row.owner_user_id)
            .with_for_update()
            .all()
        )
        for child in rows:
            children[getattr(child, identity)] = {
                c.name: str(getattr(child, c.name)) for c in child.__table__.columns
            }
    return hashlib.sha256(
        json.dumps({"manifest": snap["revision"], "components": children}, sort_keys=True).encode()
    ).hexdigest()


def _owned(db, user, kind, iid):
    if not user:
        raise PermissionError("authentication required")
    row = copies._model(db, kind, iid)
    if row is None or row.owner_user_id != user:
        raise PermissionError("installation does not exist or is not owned by current user")
    if kind == "skill" and row.source_plugin:
        raise ValueError("plugin component must be managed through its plugin")
    return row


def _view(db, user, kind, row):
    iid = row.skill_id if kind == "skill" else row.install_id
    snap = copies._snapshot(db, user, kind, iid)[0]
    revision = _revision(db, kind, row, snap)
    return {
        "ok": True,
        "install_id": iid,
        "revision": revision,
        "source": "cloud",
        "name": row.display_name if kind == "skill" else row.name,
        "version": row.version,
        "description": row.description,
        "components": row.component_ids if kind == "plugin" else {},
        "files": list(snap["files"]),
    }


def get(user_id, kind, install_id):
    with SessionLocal() as db:
        return _view(db, user_id, kind, _owned(db, user_id, kind, install_id))


def list_installed(user_id, kind):
    if not user_id:
        raise PermissionError("authentication required")
    model = AdminSkill if kind == "skill" else InstalledPlugin
    with SessionLocal() as db:
        query = db.query(model).filter(model.owner_user_id == user_id)
        if kind == "skill":
            query = query.filter(AdminSkill.source_plugin.is_(None))
        return {"ok": True, "items": [_view(db, user_id, kind, row) for row in query.all()]}


def _skill_files(root, user_id, key=None):
    from core.agent_skills.registry import _split_frontmatter, _load_skill_metadata_from_str
    from core.services.marketplace_service import compute_install_id, _rewrite_frontmatter_name

    files = {n: encode_upload(n, p.read_bytes()) for n, p in archive.iter_files(root)}
    md = files.get("SKILL.md", "")
    fm, _ = _split_frontmatter(md)
    _load_skill_metadata_from_str(md, str(fm.get("name") or ""))
    key = key or compute_install_id(str(fm["name"]), user_id)
    files["SKILL.md"] = _rewrite_frontmatter_name(md, key)
    return key, files


def install(user_id, kind, source):
    with artifact_package(user_id, source) as root:
        if kind == "skill":
            key, files = _skill_files(root, user_id)
            before = copies.snapshot(user_id, kind, key)
            if before["exists"]:
                if before["files"] != files:
                    raise ValueError("name_conflict: use update_skill")
                return get(user_id, kind, key)
            result = copies.commit(
                user_id,
                kind,
                key,
                {"files": files, "create_only": True, "request_id": uuid.uuid4().hex},
            )
            if not result["applied"]:
                raise ValueError("package could not be activated")
        else:
            from core.plugins import management as ps
            from core.plugins.packaging.importer import normalize_plugin_dir

            np = normalize_plugin_dir(root)
            key = plugin_sources._make_plugin_install_id(np.slug, user_id)
            with SessionLocal() as db:
                _permission(db, user_id, "can_import_plugin")
                if copies._model(db, kind, key) is not None:
                    raise ValueError("name_conflict: use update_plugin")
                ps.import_plugin(db, root, owner_user_id=user_id, created_by=user_id)
    return get(user_id, kind, key)


def _permission(db, user_id, flag):
    from core.auth.capabilities import resolve_user_capabilities

    if not user_id or not resolve_user_capabilities(db, user_id).get(flag):
        raise PermissionError("management permission denied")


def update(user_id, kind, install_id, source, expected_revision):
    with artifact_package(user_id, source) as root:
        with SessionLocal() as db:
            _write_lock(db)
            row = _owned(db, user_id, kind, install_id)
            before = copies._snapshot(db, user_id, kind, install_id)[0]
            if not expected_revision or _revision(db, kind, row, before) != expected_revision:
                raise ValueError("revision_conflict")
            if kind == "plugin":
                from core.plugins import management as ps
                from core.plugins.packaging.importer import normalize_plugin_dir

                _permission(db, user_id, "can_import_plugin")
                if normalize_plugin_dir(root).slug != row.slug:
                    raise ValueError("plugin slug cannot change during update")
                ps.import_plugin(db, root, owner_user_id=user_id, created_by=user_id)
            else:
                key, files = _skill_files(root, user_id, install_id)
        if kind == "skill":
            copies.commit(
                user_id,
                kind,
                key,
                {
                    "files": files,
                    "expected_revision": expected_revision,
                    "expected_model_version": before["model_version"],
                    "request_id": hashlib.sha256(
                        (user_id + key + expected_revision + copies.revision(files)).encode()
                    ).hexdigest(),
                },
            )
    return get(user_id, kind, install_id)


def uninstall(user_id, kind, install_id, expected_revision):
    with SessionLocal() as db:
        _write_lock(db)
        row = _owned(db, user_id, kind, install_id)
        before = copies._snapshot(db, user_id, kind, install_id)[0]
        if not expected_revision or _revision(db, kind, row, before) != expected_revision:
            raise ValueError("revision_conflict")
        _permission(db, user_id, "can_add_skill" if kind == "skill" else "can_import_plugin")
        record = db.get(ContentBlock, copies._id("cap-cloud-copy:", user_id, kind, install_id))
        if record:
            db.delete(record)
        if kind == "plugin":
            from core.plugins.management import uninstall_plugin

            uninstall_plugin(db, install_id, owner_user_id=user_id)
        else:
            db.delete(row)
            db.commit()
    from core.agent_skills.cache_refresh import refresh_skill_caches

    refresh_skill_caches()
    return {"ok": True, "install_id": install_id, "action": "uninstalled", "source": "cloud"}
