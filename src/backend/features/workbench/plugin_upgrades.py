"""Atomic workspace upgrades: explicit expansion approval, verified old version fallback."""

from copy import deepcopy
from hashlib import sha256
import json
import io
import zipfile
from pathlib import Path

from features.workbench.external import fail
from features.workbench.coordination import configuration
from core.db.models import ContentBlock, InstalledPlugin
from core.infra.time import as_utc, utc_now
from core.capabilities.plugins.packaging.runtime_assets import materialize, retain, pack

PREFIX = "workbench_plugin_upgrade:"
HISTORY = "workbench_plugin_revision:"


def binding_token(row):
    stamp = as_utc(row.created_at).strftime("%Y%m%dT%H%M%S.%fZ")
    return sha256((row.install_id + ":" + stamp).encode()).hexdigest()


def history_id(row, revision):
    # ContentBlock IDs are capped at 128 characters in PostgreSQL.
    return HISTORY + sha256((binding_token(row) + ":" + revision).encode()).hexdigest()


def current_revision(row):
    return (row.import_report or {}).get("package_revision") or row.version or ""


def owned(db, install_id, owner_user_id):
    row = db.get(InstalledPlugin, install_id)
    if row is None or row.owner_user_id != owner_user_id:
        fail(404, "plugin_upgrade_not_found")
    return row


def record_id(row):
    return PREFIX + binding_token(row)


def permission_set(ui):
    contributes = (ui or {}).get("contributes") or {}
    result = set()
    for app in contributes.get("applications", []):
        result.update("app:" + app["id"] + ":" + p for p in app.get("permissions", []))
        if app.get("lifecycle", {}).get("keepAlive"):
            result.add("app:" + app["id"] + ":keepAlive")
    for module in contributes.get("modules", []):
        result.update("module:" + module["id"] + ":" + g for g in module.get("grants", []))
        # A same-ID data source/resource target change is also a permission change.
        if module.get("resource"):
            digest = sha256(json.dumps(module["resource"], sort_keys=True).encode()).hexdigest()
            result.add("resource:" + module["id"] + ":" + digest)
    for source in contributes.get("data_sources", []):
        digest = sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
        result.add("data_source:" + source["id"] + ":" + digest)
    return result


def schema_changes(old_ui, new_ui):
    def versions(ui):
        return {
            a["id"]: a.get("lifecycle", {}).get("stateSchemaVersion", "1")
            for a in (ui or {}).get("contributes", {}).get("applications", [])
        }

    old, new = versions(old_ui), versions(new_ui)
    return [
        {"applicationId": k, "previous": old[k], "next": v}
        for k, v in new.items()
        if k in old and old[k] != v
    ]


def verify_package(np):
    """Reject incomplete upgrades before mutating any installed component."""
    ui = np.ui or {}
    if any(d.get("type", "").startswith("ui.") for d in np.dropped):
        fail(409, "host_incompatible")
    if ui.get("version") != 3:
        return
    from features.workbench.plugin_catalog import WORKSPACE_CAPABILITIES
    from core.capabilities.plugins.resources.installation import safe_path

    for app in ui.get("contributes", {}).get("applications", []):
        required = set(app.get("compatibility", {}).get("requiredCapabilities", []))
        if required - WORKSPACE_CAPABILITIES:
            fail(409, "host_incompatible")
    for module in ui.get("contributes", {}).get("modules", []):
        if module.get("surface") == "workspace":
            if not np.package_dir or not safe_path(Path(np.package_dir), module["entry"]).is_file():
                fail(409, "package_asset_missing")
            # Verify the exact HTML and all bundled subassets before activation.
            from features.workbench.workspace_page import render

            render(Path(np.package_dir) / "web", module["entry"][4:])


def public(row, pending):
    value = deepcopy(pending.payload if pending else {})
    return {
        "installId": row.install_id,
        "currentRevision": current_revision(row),
        "candidateRevision": value.get("candidateRevision"),
        "status": value.get("status", "current"),
        "addedPermissions": value.get("addedPermissions", []),
        "stateSchemaChanges": value.get("stateSchemaChanges", []),
        "reason": value.get("reason"),
    }


def read(db, install_id, owner_user_id):
    if install_id.startswith("plugin:local:") and owner_user_id is not None:
        from features.workbench import local_plugin_upgrades

        return local_plugin_upgrades.read(db, owner_user_id, install_id)
    row = owned(db, install_id, owner_user_id)
    from features.workbench.plugin_candidate_storage import summary

    return {
        **public(row, db.get(ContentBlock, record_id(row))),
        "candidateStorage": summary(db, binding_token(row), row.owner_user_id),
    }


def prepare(db, row, np, source, approved_revision=None):
    if row is None:
        return None
    old_ui = row.ui_contributions or {}
    if old_ui.get("version") != 3 and (np.ui or {}).get("version") != 3:
        return None
    verify_package(np)
    from features.workbench import plugin_candidate_storage as storage

    packed = pack(Path(np.package_dir)) if np.package_dir and np.ui else None
    revision = packed[0] if packed else None
    if packed:
        with zipfile.ZipFile(io.BytesIO(packed[1])) as archive:
            retained_bytes = len(packed[1]) + sum(item.file_size for item in archive.infolist())
        storage.reserve(
            db, binding_token(row), revision, retained_bytes, "cloud", row.slug, row.owner_user_id
        )
        retain(db, Path(np.package_dir), prepared=packed)
    added = sorted(permission_set(np.ui) - permission_set(old_ui))
    changed = schema_changes(old_ui, np.ui)
    if (added or changed) and revision != approved_revision:
        value = {
            "status": "pending",
            "expectedRevision": current_revision(row),
            "candidateRevision": revision,
            "addedPermissions": added,
            "stateSchemaChanges": changed,
            "source": source,
        }
        pending = db.get(ContentBlock, record_id(row))
        storage.replace(
            db,
            binding_token(row),
            pending.payload.get("candidateRevision") if pending else None,
            revision,
        )
        if pending:
            pending.payload = value
        else:
            db.add(ContentBlock(id=record_id(row), payload=value, updated_at=utc_now()))
        db.commit()
        return {
            "install_id": row.install_id,
            "slug": row.slug,
            "name": row.name,
            "kind": np.kind,
            "action": "approval_required",
            "import_report": {"package_revision": current_revision(row)},
            "upgrade": public(row, db.get(ContentBlock, record_id(row))),
        }
    remember(db, row)
    pending = db.get(ContentBlock, record_id(row))
    if pending:
        previous = pending.payload.get("candidateRevision")
        if previous and previous != revision:
            storage.change(db, binding_token(row), previous, "discarded")
        db.delete(pending)
    if revision:
        storage.change(db, binding_token(row), revision, "current")
    return None


def remember(db, row):
    revision = current_revision(row)
    identifier = history_id(row, revision)
    if db.get(ContentBlock, identifier) is None:
        db.add(
            ContentBlock(
                id=identifier,
                payload={
                    "revision": revision,
                    "ui": deepcopy(row.ui_contributions),
                    "slug": row.slug,
                    "installId": row.install_id,
                },
                updated_at=utc_now(),
            )
        )


def decide(
    db, install_id, owner_user_id, expected, candidate, decision, reset_state=False, secrets=None
):
    if install_id.startswith("plugin:local:") and owner_user_id is not None:
        from features.workbench import local_plugin_upgrades

        return local_plugin_upgrades.decide(
            owner_user_id, install_id, expected, candidate, decision, reset_state
        )
    configuration(db)
    db.expire_all()
    row = owned(db, install_id, owner_user_id)
    pending = db.get(ContentBlock, record_id(row))
    if pending is None or pending.payload.get("status") != "pending":
        db.rollback()
        fail(409, "upgrade_not_pending")
    value = pending.payload
    if (
        current_revision(row) != expected
        or value["expectedRevision"] != expected
        or value["candidateRevision"] != candidate
    ):
        db.rollback()
        fail(409, "revision_conflict")
    if decision == "reject":
        from features.workbench import plugin_candidate_storage as storage

        storage.change(db, binding_token(row), candidate, "discarded")
        pending.payload = {**value, "status": "rejected"}
        storage.collect(db, binding_token(row))
        db.commit()
        return public(row, pending)
    if value["stateSchemaChanges"] and not reset_state:
        db.rollback()
        fail(409, "state_reset_confirmation_required")
    from core.capabilities.plugins.packaging.importer import normalize_plugin_dir
    from core.capabilities.plugins.management.installation import apply_normalized as _apply_normalized

    try:
        np = normalize_plugin_dir(materialize(db, candidate))
        if np.slug != row.slug:
            fail(409, "revision_conflict")
        return _apply_normalized(
            db,
            np,
            owner_user_id=owner_user_id,
            secrets=secrets or {},
            source=value["source"],
            approved_revision=candidate,
        )
    except BaseException:
        db.rollback()
        raise
