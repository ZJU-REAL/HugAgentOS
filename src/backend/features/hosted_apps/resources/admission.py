"""Application resource admission uses the existing worker, quota and janitor."""

import hashlib
import json
import time

from fastapi import HTTPException

from core.capabilities.plugins.resources import service, store
from core.capabilities.plugins.resources.owners import application_binding
from core.capabilities.plugins.resources.reservation import lock_owner
from core.db.threads import run_in_session


def reserve(db, selected, module_id, user_id, binding):
    """One idempotent admission attempt under the owner lock (see ``service._admit``)."""
    lock_owner(db, user_id)
    row = db.get(store.PluginResource, binding["resource_id"])
    if row is None:
        return "reserve", None  # caller retains the quota/admission lock
    if (
        row.user_id != user_id
        or row.request_digest != binding["request_digest"]
        or row.application_key != binding["application_key"]
        or row.instance_id != binding["instance_id"]
        or row.install_id != selected.install_id
        or row.revision != selected.revision
        or row.module_id != module_id
    ):
        db.rollback()
        raise HTTPException(409, "idempotency_conflict")
    if row.status == "active":
        service.authorized(db, row.resource_id, user_id)
        db.commit()
        return "adopted", row
    if row.status != "starting" or row.expires_at <= time.time():
        db.commit()
        raise HTTPException(410, "resource_closed_no_replay")
    db.commit()
    return "wait", None


def _record_quota_denied(db, user_id, item, instance_id):
    from core.harness.registry.resource_owners import resource_owner

    resource_owner().record(db, user_id, "resource", "quota_denied", item=item, instance_id=instance_id)


async def create(
    user_id, key, instance_id, install_id, revision, execution_scope, module_id, idempotency_key
):
    if not idempotency_key or len(idempotency_key) > 128:
        raise HTTPException(400, "idempotency_key_required")
    item = await run_in_session(
        application_binding, user_id, key, install_id, revision, execution_scope, module_id
    )
    payload = [key, instance_id, install_id, revision, execution_scope, module_id]
    digest = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
    resource_id = hashlib.sha256(
        json.dumps([user_id, key, instance_id, idempotency_key]).encode()
    ).hexdigest()
    try:
        return await service.create(
            module_id=module_id,
            user_id=user_id,
            chat_id=None,
            slug=item["binding"]["slug"],
            install_id=install_id,
            _application={
                "application_key": key,
                "instance_id": instance_id,
                "revision": revision,
                "scope": execution_scope,
                "request_digest": digest,
                "resource_id": resource_id,
            },
        )
    except HTTPException as error:
        if error.status_code == 429:
            try:
                await run_in_session(_record_quota_denied, user_id, item, instance_id)
            except Exception:
                pass  # The original quota denial remains authoritative if telemetry storage fails.
        raise


def owned(db, user_id, key, instance_id, resource_id, *, allow_closed=False):
    row = store.get(db, resource_id, user_id)
    if row.application_key != key or row.instance_id != instance_id or row.chat_id is not None:
        raise HTTPException(404, "resource_unavailable")
    if allow_closed:
        # Revoked grants stop all use; authenticated owners may still release their worker.
        return row
    return service.authorized(db, resource_id, user_id)


def _extend_lease(db, user_id, key, instance_id, resource_id):
    """Extend a foreground lease; returns True when the background lease owns it instead."""
    from core.harness.registry.resource_owners import resource_owner

    row = owned(db, user_id, key, instance_id, resource_id)
    if resource_owner().authorize(db, row):
        return True
    updated = (
        db.query(store.PluginResource)
        .filter_by(resource_id=row.resource_id, status="active")
        .filter(store.PluginResource.expires_at > time.time())
        .update({"expires_at": time.time() + 90}, synchronize_session=False)
    )
    db.commit()
    if updated != 1:
        raise HTTPException(410, "resource_expired")
    return False


def _public(db, user_id, resource_id):
    return store.public(store.get(db, resource_id, user_id))


async def renew(user_id, key, instance_id, resource_id):
    from core.capabilities.plugins.resources.deadlines import arm
    from core.harness.registry.resource_owners import resource_owner

    if await run_in_session(_extend_lease, user_id, key, instance_id, resource_id):
        await resource_owner().renew(user_id, key, instance_id, resource_id)
    else:
        await arm(resource_id)
    return await run_in_session(_public, user_id, resource_id)


def _application_rows(db, user_id, application_key, instance_id, install_id):
    query = db.query(store.PluginResource).filter(
        store.PluginResource.user_id == user_id,
        store.PluginResource.application_key.is_not(None),
        store.PluginResource.status != "closed",
    )
    for name, value in (
        ("application_key", application_key),
        ("instance_id", instance_id),
        ("install_id", install_id),
    ):
        if value is not None:
            query = query.filter(getattr(store.PluginResource, name) == value)
    rows = query.all()
    db.expunge_all()
    return rows


async def close_application_resources(
    user_id, *, application_key=None, instance_id=None, install_id=None
):
    rows = await run_in_session(_application_rows, user_id, application_key, instance_id, install_id)
    failures = []
    for row in rows:
        try:
            await service.close(row)
        except Exception:
            failures.append(row.resource_id)
    return {"closed": not failures, "retry_resource_ids": failures}
