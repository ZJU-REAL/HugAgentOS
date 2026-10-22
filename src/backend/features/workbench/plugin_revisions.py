"""Exact revision leases. Retention never bypasses current identity or permission checks."""

from copy import deepcopy
from hashlib import sha256
import time

from features.workbench import catalog
from features.workbench.external import fail
from features.workbench.coordination import configuration, runtime
from features.workbench.plugin_upgrades import HISTORY, history_id, binding_token, current_revision, permission_set
from core.db.models import ContentBlock, InstalledPlugin
from core.infra.time import utc_now
from core.capabilities.plugins.packaging.runtime_assets import materialize
from sqlalchemy import or_

from core.capabilities.plugins.resources.installation import Installation
from features.workbench.installation_binding import exact as resolve

LEASE = "workbench_plugin_lease:"
LEASE_SECONDS = 90


def lease_id(user_id, key, revision, instance_id):
    return (
        LEASE
        + sha256((user_id + ":" + key + ":" + revision + ":" + instance_id).encode()).hexdigest()
    )


def current(db, user_id, key):
    item = catalog.readable(db, user_id, key)
    if item["declaration"]["target"]["kind"] != "plugin":
        fail(404, "app_not_found")
    if not item["availability"]["available"]:
        fail(409, "app_disabled")
    binding = item["binding"]
    selected = resolve(db, binding["slug"], user_id, binding["installId"])
    row = db.get(InstalledPlugin, selected.install_id)
    if row is None:
        from features.desktop.capabilities import registry

        row = registry.get(selected.install_id, db=db)
        if row is None or row.profile_id != "local" or row.state == "removed":
            fail(409, "origin_unavailable")
    return item, row, selected


def create(db, user_id, key, revision, instance_id):
    runtime(db, user_id)
    db.expire_all()
    _, row, selected = current(db, user_id, key)
    if revision != selected.revision:
        db.rollback()
        fail(409, "revision_conflict")
    identifier = lease_id(user_id, key, revision, instance_id)
    record = db.get(ContentBlock, identifier)
    if record and record.payload["closed"]:
        db.rollback()
        fail(410, "instance_closed")
    active = (
        db.query(ContentBlock)
        .filter(
            ContentBlock.id.startswith(LEASE, autoescape=True),
            ContentBlock.payload["userId"].as_string() == user_id,
        )
        .all()
    )
    if (
        record is None
        and sum(
            r.payload["userId"] == user_id
            and not r.payload["closed"]
            and r.payload["expiresAt"] > time.time()
            for r in active
        )
        >= 12
    ):
        db.rollback()
        fail(429, "instance_quota_exceeded")
    value = {
        "userId": user_id,
        "applicationKey": key,
        "revision": revision,
        "installId": row.install_id,
        "bindingToken": binding_token(row),
        "instanceId": instance_id,
        "closed": False,
        "expiresAt": time.time() + LEASE_SECONDS,
    }
    if record:
        record.payload = value
    else:
        db.add(ContentBlock(id=identifier, payload=value, updated_at=utc_now()))
    db.commit()
    return {"instanceId": instance_id, "revision": revision, "expiresAt": value["expiresAt"]}


def active_lease(db, user_id, key, revision, instance_id, row):
    """Exact identity must stay active for current and retained revision calls."""
    if not instance_id:
        fail(403, "instance_binding_mismatch")
    lease = db.get(ContentBlock, lease_id(user_id, key, revision, instance_id))
    value = lease.payload if lease else {}
    if (
        value.get("closed", True)
        or value.get("expiresAt", 0) <= time.time()
        or value.get("userId") != user_id
        or value.get("applicationKey") != key
        or value.get("revision") != revision
        or value.get("instanceId") != instance_id
        or value.get("installId") != row.install_id
        or value.get("bindingToken") != binding_token(row)
    ):
        fail(410, "instance_expired")
    return lease


def resolve_revision(db, user_id, key, revision, instance_id=None):
    item, row, selected = current(db, user_id, key)
    if selected.revision == revision:
        if instance_id:
            active_lease(db, user_id, key, revision, instance_id, row)
        return selected
    if not instance_id:
        fail(409, "revision_conflict")
    active_lease(db, user_id, key, revision, instance_id, row)
    history = db.get(ContentBlock, history_id(row, revision))
    if history is None:
        fail(410, "revision_expired")
    previous = history.payload
    if permission_set(previous["ui"]) - permission_set(selected.ui):
        fail(403, "permission_revoked")
    # Current app must still reference a declared application in the old package.
    app_id = item["declaration"]["id"]
    if not any(
        a["id"] == app_id
        for a in (previous["ui"] or {}).get("contributes", {}).get("applications", [])
    ):
        fail(404, "app_not_found")
    if previous.get("executionScope") == "local":
        from features.desktop.capabilities import store

        component = store.get("plugin", "local", row.key, revision)
        if component is None:
            fail(410, "revision_expired")
        package = component.path / "package"
    else:
        package = materialize(db, revision)
    return Installation(
        selected.install_id, revision, selected.slug, deepcopy(previous["ui"]), package
    )


def renew(db, user_id, key, revision, instance_id):
    runtime(db, user_id)
    db.expire_all()
    resolve_revision(db, user_id, key, revision, instance_id)
    record = db.get(ContentBlock, lease_id(user_id, key, revision, instance_id))
    if record is None or record.payload["closed"] or record.payload["expiresAt"] <= time.time():
        db.rollback()
        fail(410, "instance_expired")
    record.payload = {**record.payload, "expiresAt": time.time() + LEASE_SECONDS}
    db.commit()
    return {
        "instanceId": instance_id,
        "revision": revision,
        "expiresAt": record.payload["expiresAt"],
    }


def close(db, user_id, key, revision, instance_id):
    runtime(db, user_id)
    db.expire_all()
    identifier = lease_id(user_id, key, revision, instance_id)
    record = db.get(ContentBlock, identifier)
    if record:
        # Keep expiresAt: closed instances do not force early asset collection.
        record.payload = {**record.payload, "closed": True}
    from features.workbench.capabilities import revoke_matching

    revoke_matching(db, user_id, {key}, revision, instance_id)
    db.commit()
    return {"closed": True}


def collect(db):
    """Called by the existing resource janitor. Only history-managed archives are collected."""
    from core.db.models.plugin_resource import PluginResource, PluginResourcePackage

    configuration(db)
    leases = db.query(ContentBlock).filter(ContentBlock.id.startswith(LEASE, autoescape=True)).all()
    retained = {r.payload["revision"] for r in leases if r.payload["expiresAt"] > time.time()}
    retained.update(current_revision(r) for r in db.query(InstalledPlugin).all())
    from features.desktop.models import DeviceCapabilityInstallation

    retained.update(
        r.resolved_revision
        for r in db.query(DeviceCapabilityInstallation)
        .filter(DeviceCapabilityInstallation.state != "removed")
        .all()
    )
    retained.update(
        r.revision
        for r in db.query(PluginResource)
        .filter(or_(PluginResource.expires_at > time.time(), PluginResource.status != "closed"))
        .all()
    )
    from features.workbench.plugin_upgrades import PREFIX

    pending = (
        db.query(ContentBlock).filter(ContentBlock.id.startswith(PREFIX, autoescape=True)).all()
    )
    retained.update(
        r.payload.get("candidateRevision") for r in pending if r.payload.get("status") == "pending"
    )
    from features.workbench.plugin_candidate_storage import records

    retained.update(
        r.payload["revision"]
        for r in records(db)
        if r.payload["status"] == "pending"
        or (r.payload["status"] == "reserved" and r.payload["expiresAt"] > time.time())
    )
    histories = (
        db.query(ContentBlock).filter(ContentBlock.id.startswith(HISTORY, autoescape=True)).all()
    )
    candidates = {r.payload["revision"] for r in histories} - retained
    for history in histories:
        if history.payload["revision"] in candidates:
            if history.payload.get("executionScope") == "local":
                from features.desktop.capabilities import store

                store.remove_revision(
                    "plugin", "local", history.payload["slug"], history.payload["revision"]
                )
            db.delete(history)
    for revision in candidates:
        package = db.get(PluginResourcePackage, revision)
        if package:
            db.delete(package)
    for lease in leases:
        if lease.payload["expiresAt"] <= time.time():
            db.delete(lease)
    db.commit()
    from core.capabilities.plugins.packaging.runtime_assets import forget_cache

    for revision in candidates:
        forget_cache(revision)
    return len(candidates)
