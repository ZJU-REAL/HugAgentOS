"""Owner-bound runner timers enforce application resource leases independently of API sweeps."""

from core.applications.coordination import runtime

import hashlib
import json
from fastapi import HTTPException
from core.applications import activity, origin
from . import store
from .owners import runtime_session


def provider_for(row):
    from core.sandbox.factory import get_sandbox_provider

    provider = get_sandbox_provider()
    descriptor = store.descriptor(row)
    method = getattr(provider, "set_process_deadline", None)
    if descriptor.get("provider") != provider.name or not callable(method):
        raise HTTPException(403, detail={"code": "unsupported_capability"})
    if not descriptor.get("process_id"):
        raise HTTPException(410, detail={"code": "instance_closed"})
    return method, descriptor["process_id"]


def invalidate(db, row):
    from core.applications import external

    rid, user = row.resource_id, row.user_id
    db.commit()
    runtime(db, user)
    db.expire_all()
    if row.status != "closed":
        row.status = "closing"
        row.expires_at = 0
    current = activity.read(db, user)
    record = current["background"].get(rid)
    if record:
        record["expiresAt"] = 0
        record["revoked"] = True
    activity.save(db, user, current)  # Retain the exact charged cleanup reservation.


async def arm(db, row, *, require_background=False):
    if row.application_key is None or row.chat_id is not None:
        return  # Ordinary chat resources retain their existing lifetime.
    value = activity.read(db, row.user_id)
    record = value["background"].get(row.resource_id)
    source = origin.current(db)
    expires = row.expires_at
    if record:
        binding = record["binding"]
        if (
            record["applicationKey"] != row.application_key
            or record["instanceId"] != row.instance_id
            or binding.get("installId") != row.install_id
            or binding.get("packageRevision") != row.revision
            or binding.get("executionScope") != row.scope
            or binding.get("origin") != source
        ):
            raise HTTPException(403, detail={"code": "resource_binding_mismatch"})
        expires = min(expires, record["expiresAt"], record["deadline"])
    try:
        method, process_id = provider_for(row)
    except HTTPException as error:
        if not (require_background or record) and error.status_code == 403:
            return  # Remote providers have no independent persistent deadline contract.
        raise
    digest = hashlib.sha256(
        json.dumps(
            [
                row.resource_id,
                row.user_id,
                row.application_key,
                row.instance_id,
                row.install_id,
                row.revision,
                row.scope,
                source,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    try:
        result = await method(
            process_id,
            sandbox_session_id=runtime_session(row),
            user_id=row.user_id,
            binding=digest,
            expires_at=expires,
            absolute_deadline=record["deadline"] if record else None,
        )
    except Exception:
        # Do not leave an authorized extended lease after uncertain timer delivery.
        invalidate(db, row)
        raise HTTPException(503, detail={"code": "release_failed"}) from None
    if result.get("supported") is False:
        if record:
            invalidate(db, row)
        if require_background or record:
            raise HTTPException(403, detail={"code": "unsupported_capability"})
        return
    if result.get("armed") is not True:
        invalidate(db, row)
        raise HTTPException(503, detail={"code": "release_failed"})


async def restore(db, *, after="", limit=100):
    rows = (
        db.query(store.PluginResource)
        .filter(
            store.PluginResource.application_key.is_not(None),
            store.PluginResource.status == "active",
            store.PluginResource.resource_id > after,
        )
        .order_by(store.PluginResource.resource_id)
        .limit(limit)
        .all()
    )
    for row in rows:
        try:
            await arm(db, row)
        except HTTPException:
            db.rollback()  # Existing janitor owns persisted closing cleanup.
    return rows[-1].resource_id if len(rows) == limit else ""
