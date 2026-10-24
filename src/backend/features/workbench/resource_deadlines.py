"""Workbench leases bind process deadlines to application ownership."""
import hashlib
import json
from fastapi import HTTPException
from features.workbench.coordination import runtime
from features.workbench import activity, origin
from core.capabilities.plugins.resources.deadlines import provider_for
from core.capabilities.plugins.resources.owners import runtime_session

def invalidate(db, row):
    from features.workbench import external

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


def _arming(db, resource_id, require_background):
    """Validate the lease record and resolve the runner timer; None when nothing to arm."""
    from core.capabilities.plugins.resources import store

    row = db.get(store.PluginResource, resource_id)
    if row is None or row.application_key is None or row.chat_id is not None:
        return None  # Ordinary chat resources retain their existing lifetime.
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
            return None  # Remote providers have no independent persistent deadline contract.
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
    return {
        "method": method,
        "process_id": process_id,
        "session": runtime_session(row),
        "user_id": row.user_id,
        "digest": digest,
        "expires": expires,
        "deadline": record["deadline"] if record else None,
        "background": bool(record),
    }


def _invalidate(db, resource_id):
    from core.capabilities.plugins.resources import store

    invalidate(db, db.get(store.PluginResource, resource_id))


async def arm(resource_id, *, require_background=False):
    """Hand the lease deadline to the runner; revoke the lease if delivery is uncertain.

    The lease is read and, on failure, invalidated on sessions of their own; the
    runner call in between holds no connection.
    """
    from core.db.threads import run_in_session

    timer = await run_in_session(_arming, resource_id, require_background)
    if timer is None:
        return
    try:
        result = await timer["method"](
            timer["process_id"],
            sandbox_session_id=timer["session"],
            user_id=timer["user_id"],
            binding=timer["digest"],
            expires_at=timer["expires"],
            absolute_deadline=timer["deadline"],
        )
    except Exception:
        # Do not leave an authorized extended lease after uncertain timer delivery.
        await run_in_session(_invalidate, resource_id)
        raise HTTPException(503, detail={"code": "release_failed"}) from None
    if result.get("supported") is False:
        if timer["background"]:
            await run_in_session(_invalidate, resource_id)
        if require_background or timer["background"]:
            raise HTTPException(403, detail={"code": "unsupported_capability"})
        return
    if result.get("armed") is not True:
        await run_in_session(_invalidate, resource_id)
        raise HTTPException(503, detail={"code": "release_failed"})
