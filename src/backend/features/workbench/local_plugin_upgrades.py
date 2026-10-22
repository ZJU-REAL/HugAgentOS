"""Device upgrades use the existing immutable store and management transaction lock."""

from copy import deepcopy

from features.workbench.external import fail
from features.workbench.plugin_upgrades import PREFIX, history_id, binding_token, permission_set, schema_changes, verify_package
from features.desktop.capabilities import registry, store
from core.harness.registry.run_capabilities import has_run_capabilities
from core.capabilities.identity_rules import LOCAL_PROFILE
from features.desktop.capabilities.plugins import load_manifest
from core.db.models import ContentBlock
from core.infra.time import utc_now
from core.capabilities.plugins.management.package import serialized


def owned(db, user_id, install_id):
    from features.desktop.local.plugins.service import _owned

    if not has_run_capabilities():
        fail(404, "plugin_upgrade_not_found")
    try:
        return _owned(user_id, install_id, db)
    except PermissionError:
        fail(404, "plugin_upgrade_not_found")


def identifier(inst):
    return PREFIX + binding_token(inst)


def public(inst, record):
    value = record.payload if record else {}
    return {
        "installId": inst.install_id,
        "currentRevision": inst.resolved_revision,
        "candidateRevision": value.get("candidateRevision"),
        "status": value.get("status", "current"),
        "addedPermissions": value.get("addedPermissions", []),
        "stateSchemaChanges": value.get("stateSchemaChanges", []),
    }


def read(db, user_id, install_id):
    inst = owned(db, user_id, install_id)
    from features.workbench.plugin_candidate_storage import summary

    return {
        **public(inst, db.get(ContentBlock, identifier(inst))),
        "candidateStorage": summary(db, binding_token(inst), user_id),
    }


def prepare(db, inst, prepared, approved_revision=None):
    np, key, definition, digest, comp, children = prepared
    previous = store.get("plugin", LOCAL_PROFILE, inst.key, inst.resolved_revision)
    old_ui = load_manifest(previous).get("ui_contributions") if previous else None
    if (old_ui or {}).get("version") != 3 and (np.ui or {}).get("version") != 3:
        return None
    # The immutable component survives the input snapshot.
    np.package_dir = str(comp.path / "package")
    verify_package(np)
    added = sorted(permission_set(np.ui) - permission_set(old_ui))
    changes = schema_changes(old_ui, np.ui)
    from features.workbench import plugin_candidate_storage as storage

    record = db.get(ContentBlock, identifier(inst))
    if (added or changes) and comp.revision != approved_revision:
        value = {
            "status": "pending",
            "expectedRevision": inst.resolved_revision,
            "candidateRevision": comp.revision,
            "contentHash": digest,
            "addedPermissions": added,
            "stateSchemaChanges": changes,
        }
        storage.replace(
            db,
            binding_token(inst),
            record.payload.get("candidateRevision") if record else None,
            comp.revision,
        )
        if record:
            record.payload = value
        else:
            db.add(ContentBlock(id=identifier(inst), payload=value, updated_at=utc_now()))
        return {
            "ok": True,
            "install_id": inst.install_id,
            "slug": inst.key,
            "name": inst.display_name,
            "source": "local",
            "action": "approval_required",
            "upgrade": public(inst, db.get(ContentBlock, identifier(inst))),
        }
    history_identifier = history_id(inst, inst.resolved_revision)
    if db.get(ContentBlock, history_identifier) is None:
        db.add(
            ContentBlock(
                id=history_identifier,
                payload={
                    "revision": inst.resolved_revision,
                    "ui": deepcopy(old_ui),
                    "slug": inst.key,
                    "installId": inst.install_id,
                    "executionScope": "local",
                },
                updated_at=utc_now(),
            )
        )
    if record:
        previous_revision = record.payload.get("candidateRevision")
        if previous_revision and previous_revision != comp.revision:
            storage.change(db, binding_token(inst), previous_revision, "discarded")
        db.delete(record)
    storage.change(db, binding_token(inst), comp.revision, "current")
    return None


@serialized
def decide(user_id, install_id, expected, candidate, decision, reset_state):
    from features.desktop.local.plugins import service as service

    with registry._session() as db:
        inst = owned(db, user_id, install_id)
        record = db.get(ContentBlock, identifier(inst))
        if not record or record.payload.get("status") != "pending":
            fail(409, "upgrade_not_pending")
        value = record.payload
        if (
            inst.resolved_revision != expected
            or value["expectedRevision"] != expected
            or value["candidateRevision"] != candidate
        ):
            fail(409, "revision_conflict")
        if decision == "reject":
            from features.workbench import plugin_candidate_storage as storage

            storage.change(db, binding_token(inst), candidate, "discarded")
            record.payload = {**value, "status": "rejected"}
            storage.collect(db, binding_token(inst))
            return public(inst, record)
        if value["stateSchemaChanges"] and not reset_state:
            fail(409, "state_reset_confirmation_required")
        comp = store.get("plugin", LOCAL_PROFILE, inst.key, candidate)
        if comp is None:
            fail(409, "package_asset_missing")
        ids = inst.payload.get("component_identity_map", {})
        prepared = service._prepare(
            user_id,
            comp.path / "package",
            inst.key,
            skill_ids=ids.get("skills"),
            server_ids=ids.get("mcp"),
        )
        if prepared[3] != value["contentHash"]:
            fail(409, "revision_conflict")
        service._claim(db, inst, expected)
        prepare(db, inst, prepared, approved_revision=candidate)
        service._publish(user_id, prepared, db)
    service.skill_service._invalidate()
    return service.get(user_id, install_id)
