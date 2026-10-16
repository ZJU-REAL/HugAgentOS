"""Separate background permission and budget for existing application resources."""

from core.applications.coordination import runtime


import time
import logging
from core.applications import activity, external
from core.plugins.resources import service, store

MAX_PER_USER = 2
MAX_PER_APP = 1
LEASE_SECONDS = 90
MAX_RUN_SECONDS = 900


def resource(db, resource_id, user_id, key, instance_id):
    row = service.authorized(db, resource_id, user_id)
    if (
        getattr(row, "application_key", None) != key
        or getattr(row, "instance_id", None) != instance_id
        or row.chat_id is not None
    ):
        external.fail(403, "resource_owner_mismatch")
    return row


def check_binding(row, expected):
    if (
        row.install_id != expected.get("installId")
        or row.revision != expected.get("packageRevision")
        or row.scope != expected.get("executionScope")
    ):
        external.fail(403, "resource_binding_mismatch")


def admission(db, user_id, key, instance_id, resource_id):
    # Module identity comes from the registered instance, never the request body.
    registered = activity.read(db, user_id)["instances"].get(instance_id, {})
    value, instance, _ = activity.authorize(
        db, user_id, key, instance_id, "background", registered.get("moduleId")
    )
    row = resource(db, resource_id, user_id, key, instance_id)
    check_binding(row, instance["binding"])
    if row.module_id != instance.get("moduleId"):
        external.fail(403, "resource_module_mismatch")
    # Recheck the capability against the exact owned resource module.
    value, instance, _ = activity.authorize(
        db, user_id, key, instance_id, "background", row.module_id
    )
    return value, instance, row


async def start(db, user_id, key, instance_id, resource_id):
    from core.plugins.resources.deadlines import arm, provider_for

    # Probe the runner without holding a database write/coordination lock.
    _, _, probe = admission(db, user_id, key, instance_id, resource_id)
    provider_for(probe)
    await arm(db, probe, require_background=True)
    runtime(db, user_id)
    db.expire_all()
    # Authorization and quotas are evaluated again after the probe and owner lock.
    value, instance, row = admission(db, user_id, key, instance_id, resource_id)
    now = time.time()
    existing = value["background"].get(resource_id)
    if existing:
        if existing["expiresAt"] <= now or existing["deadline"] <= now:
            external.fail(410, "background_expired")
        return {"resourceId": resource_id, "expiresAt": existing["expiresAt"]}
    # Expired reservations remain charged until actual stop/reconcile.
    if (
        len(value["background"]) >= MAX_PER_USER
        or sum(v["applicationKey"] == key for v in value["background"].values()) >= MAX_PER_APP
    ):
        external.fail(429, "background_quota_exhausted")
    expires = now + LEASE_SECONDS
    value["background"][resource_id] = {
        "applicationKey": key,
        "instanceId": instance_id,
        "binding": instance["binding"],
        "expiresAt": expires,
        "deadline": now + MAX_RUN_SECONDS,
    }
    row.expires_at = min(row.expires_at, expires)
    activity.save(db, user_id, value)
    await arm(db, row, require_background=True)
    return {"resourceId": resource_id, "expiresAt": expires}


def authorized_resource(db, row):
    """WB11 must call before background lease/command. Never extends a lease."""
    value = activity.read(db, row.user_id)
    record = value["background"].get(row.resource_id)
    if not record:
        return False
    now = time.time()
    if record["expiresAt"] <= now or record["deadline"] <= now:
        external.fail(410, "background_expired")
    item = activity.available(db, row.user_id, record["applicationKey"])
    grant = value["grants"].get(record["applicationKey"], {})
    if (
        activity.binding(item) != record["binding"]
        or grant.get("binding") != record["binding"]
        or "background" not in grant.get("capabilities", [])
        or "background" not in item["declaration"].get("permissions", [])
    ):
        external.fail(403, "capability_not_granted")
    check_binding(row, record["binding"])
    if (
        getattr(row, "application_key", None) != record["applicationKey"]
        or getattr(row, "instance_id", None) != record["instanceId"]
    ):
        external.fail(403, "resource_owner_mismatch")
    return True


async def renew(db, user_id, key, instance_id, resource_id):
    from core.plugins.resources.deadlines import arm

    runtime(db, user_id)
    db.expire_all()
    value = activity.read(db, user_id)
    record = value["background"].get(resource_id)
    if not record or record["applicationKey"] != key or record["instanceId"] != instance_id:
        external.fail(404, "background_not_found")
    row = resource(db, resource_id, user_id, key, instance_id)
    authorized_resource(db, row)
    expires = min(time.time() + LEASE_SECONDS, record["deadline"])
    record["expiresAt"] = expires
    row.expires_at = expires
    activity.save(db, user_id, value)
    await arm(db, row, require_background=True)
    return {"resourceId": resource_id, "expiresAt": expires}


async def stop(db, user_id, key, instance_id, resource_id):
    # Ownership, not availability, gates cleanup so revoked users can still stop.
    row = store.get(db, resource_id, user_id)
    if (
        getattr(row, "application_key", None) != key
        or getattr(row, "instance_id", None) != instance_id
    ):
        external.fail(404, "background_not_found")
    await service.close(db, row)
    runtime(db, user_id)
    db.expire_all()
    value = activity.read(db, user_id)
    value["background"].pop(resource_id, None)
    activity.save(db, user_id, value)


async def stop_all(db, user_id, application_key=None, install_id=None):
    value = activity.read(db, user_id)
    for rid, record in list(value["background"].items()):
        if (application_key is None or record["applicationKey"] == application_key) and (
            install_id is None or record["binding"].get("installId") == install_id
        ):
            await stop(db, user_id, record["applicationKey"], record["instanceId"], rid)


async def reconcile(db, user_id):
    from fastapi import HTTPException

    value = activity.read(db, user_id)
    for rid, record in list(value["background"].items()):
        try:
            row = resource(db, rid, user_id, record["applicationKey"], record["instanceId"])
            authorized_resource(db, row)
        except HTTPException:
            # Metadata is retained until real shutdown succeeds.
            row = db.get(store.PluginResource, rid)
            if row is not None and row.user_id == user_id:
                try:
                    await service.close(db, row)
                except Exception as error:
                    db.rollback()
                    logging.getLogger(__name__).warning(
                        "Background cleanup deferred: %s", type(error).__name__
                    )
                    continue
            runtime(db, user_id)
            db.expire_all()
            current = activity.read(db, user_id)
            current["background"].pop(rid, None)
            activity.save(db, user_id, current)
