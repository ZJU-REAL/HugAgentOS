"""Application resource admission uses the existing worker, quota and janitor."""

import asyncio
import hashlib
import json
import time

from fastapi import HTTPException

from . import service, store
from .owners import application_binding
from .reservation import lock_owner


async def reserve(db, selected, module_id, user_id, binding):
    deadline = time.monotonic() + 95
    while True:
        db.commit()
        lock_owner(db, user_id)
        row = db.get(store.PluginResource, binding["resource_id"])
        if row is None:
            return None, False  # caller retains the quota/admission lock
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
            return row, True
        if row.status != "starting" or row.expires_at <= time.time():
            db.commit()
            raise HTTPException(410, "resource_closed_no_replay")
        db.commit()
        if time.monotonic() >= deadline:
            raise HTTPException(503, "resource_start_pending")
        await asyncio.sleep(0.05)
        db.expire_all()
        application_binding(
            db,
            user_id,
            binding["application_key"],
            selected.install_id,
            selected.revision,
            binding["scope"],
            module_id,
        )


async def create(
    db, user_id, key, instance_id, install_id, revision, execution_scope, module_id, idempotency_key
):
    if not idempotency_key or len(idempotency_key) > 128:
        raise HTTPException(400, "idempotency_key_required")
    item = application_binding(db, user_id, key, install_id, revision, execution_scope, module_id)
    payload = [key, instance_id, install_id, revision, execution_scope, module_id]
    digest = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
    resource_id = hashlib.sha256(
        json.dumps([user_id, key, instance_id, idempotency_key]).encode()
    ).hexdigest()
    try:
        return await service.create(
            db,
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
        db.rollback()
        if error.status_code == 429:
            from core.applications.diagnostics import record

            try:
                record(db, user_id, "resource", "quota_denied", item=item, instance_id=instance_id)
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


async def renew(db, user_id, key, instance_id, resource_id):
    row = owned(db, user_id, key, instance_id, resource_id)
    from core.applications import background

    if background.authorized_resource(db, row):
        await background.renew(db, user_id, key, instance_id, resource_id)
        return store.public(row)
    updated = (
        db.query(store.PluginResource)
        .filter_by(resource_id=row.resource_id, status="active")
        .filter(store.PluginResource.expires_at > time.time())
        .update({"expires_at": time.time() + 90}, synchronize_session=False)
    )
    db.commit()
    if updated != 1:
        raise HTTPException(410, "resource_expired")
    db.refresh(row)
    from .deadlines import arm

    await arm(db, row)
    return store.public(row)


async def close_application_resources(
    db, user_id, *, application_key=None, instance_id=None, install_id=None
):
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
    failures = []
    for row in query.all():
        try:
            await service.close(db, row)
        except Exception:
            db.rollback()
            failures.append(row.resource_id)
    return {"closed": not failures, "retry_resource_ids": failures}
