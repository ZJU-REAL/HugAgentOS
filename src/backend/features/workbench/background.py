"""Separate background permission and budget for existing application resources."""

from features.workbench.coordination import runtime


import time
import logging
from features.workbench import activity, external
from core.capabilities.plugins.resources import service, store
from core.db.threads import run_in_session

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


def _probe(db, user_id, key, instance_id, resource_id):
    from core.capabilities.plugins.resources.deadlines import provider_for

    _, _, probe = admission(db, user_id, key, instance_id, resource_id)
    provider_for(probe)


def _reserve(db, user_id, key, instance_id, resource_id):
    """Charge a background reservation under the owner lock; None when one exists."""
    runtime(db, user_id)
    db.expire_all()
    # Authorization and quotas are evaluated again after the probe and owner lock.
    value, instance, row = admission(db, user_id, key, instance_id, resource_id)
    now = time.time()
    existing = value["background"].get(resource_id)
    if existing:
        if existing["expiresAt"] <= now or existing["deadline"] <= now:
            external.fail(410, "background_expired")
        db.commit()
        return existing["expiresAt"], False
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
    return expires, True


async def start(user_id, key, instance_id, resource_id):
    from core.capabilities.plugins.resources.deadlines import arm

    # Probe the runner without holding a database write/coordination lock.
    await run_in_session(_probe, user_id, key, instance_id, resource_id)
    await arm(resource_id, require_background=True)
    expires, reserved = await run_in_session(_reserve, user_id, key, instance_id, resource_id)
    if reserved:
        await arm(resource_id, require_background=True)
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


def _extend(db, user_id, key, instance_id, resource_id):
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
    return expires


async def renew(user_id, key, instance_id, resource_id):
    from core.capabilities.plugins.resources.deadlines import arm

    expires = await run_in_session(_extend, user_id, key, instance_id, resource_id)
    await arm(resource_id, require_background=True)
    return {"resourceId": resource_id, "expiresAt": expires}


def _owned_row(db, user_id, key, instance_id, resource_id):
    # Ownership, not availability, gates cleanup so revoked users can still stop.
    row = store.get(db, resource_id, user_id)
    if (
        getattr(row, "application_key", None) != key
        or getattr(row, "instance_id", None) != instance_id
    ):
        external.fail(404, "background_not_found")
    db.expunge(row)
    return row


def _release(db, user_id, resource_id):
    runtime(db, user_id)
    db.expire_all()
    value = activity.read(db, user_id)
    value["background"].pop(resource_id, None)
    activity.save(db, user_id, value)


async def stop(user_id, key, instance_id, resource_id):
    row = await run_in_session(_owned_row, user_id, key, instance_id, resource_id)
    await service.close(row)
    await run_in_session(_release, user_id, resource_id)


def _records(db, user_id):
    return list(activity.read(db, user_id)["background"].items())


async def stop_all(user_id, application_key=None, install_id=None):
    for rid, record in await run_in_session(_records, user_id):
        if (application_key is None or record["applicationKey"] == application_key) and (
            install_id is None or record["binding"].get("installId") == install_id
        ):
            await stop(user_id, record["applicationKey"], record["instanceId"], rid)


def _revoked_row(db, user_id, rid, record):
    """None while the reservation is still authorized; else the row to stop (or False if gone)."""
    from fastapi import HTTPException

    try:
        row = resource(db, rid, user_id, record["applicationKey"], record["instanceId"])
        authorized_resource(db, row)
        return None
    except HTTPException:
        db.rollback()
    # Metadata is retained until real shutdown succeeds.
    row = db.get(store.PluginResource, rid)
    if row is None or row.user_id != user_id:
        return False
    db.expunge(row)
    return row


async def reconcile(user_id):
    for rid, record in await run_in_session(_records, user_id):
        row = await run_in_session(_revoked_row, user_id, rid, record)
        if row is None:
            continue
        if row is not False:
            try:
                await service.close(row)
            except Exception as error:
                logging.getLogger(__name__).warning(
                    "Background cleanup deferred: %s", type(error).__name__
                )
                continue
        await run_in_session(_release, user_id, rid)
