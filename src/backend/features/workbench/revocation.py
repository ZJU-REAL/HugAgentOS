"""Persist credential revocation before best-effort worker cleanup."""

from features.workbench.coordination import runtime


from core.db.threads import run_in_session
import asyncio
import logging
from copy import deepcopy

from core.db.models import ContentBlock
from core.db.models.plugin_resource import (
    PluginResource,
    PluginResourceAssetTicket,
    PluginResourceTicket,
)
from core.capabilities.plugins.resources import service
from sqlalchemy import delete, or_

EXTERNAL_PREFIXES = (
    "workbench_external_ticket:",
    "workbench_external_challenge:",
    "workbench_external_intent:",
)
LIVE_STATUSES = ("active", "starting", "warming", "warm", "closing")


def _lock(db, user_id):
    runtime(db, user_id)
    db.expire_all()


def _revoke_activity(db, user_id, keys=None):
    """Stage revoked activity credentials in the caller's transaction."""
    from features.workbench import activity

    row = db.get(ContentBlock, activity.identity(user_id))
    if row is None:
        return
    value = deepcopy(row.payload)

    def matches(key):
        return keys is None or key in keys

    for collection in ("grants", "badges"):
        value[collection] = {
            key: record for key, record in value.get(collection, {}).items() if not matches(key)
        }
    value["instances"] = {
        key: record
        for key, record in value.get("instances", {}).items()
        if not matches(record["applicationKey"])
    }
    value["notifications"] = [
        record for record in value.get("notifications", []) if not matches(record["applicationKey"])
    ]
    # Failed stops remain charged until real cleanup succeeds. They have no lease.
    for record in value.get("background", {}).values():
        if matches(record["applicationKey"]):
            record.update(expiresAt=0, deadline=0, revoked=True)
    row.payload = value


def _revoke_worker_credentials(db, user_id, rows, keys=None):
    _revoke_activity(db, user_id, keys)
    from features.workbench.capabilities import revoke_matching

    revoke_matching(db, user_id, keys)
    leases = (
        db.query(ContentBlock)
        .filter(
            ContentBlock.id.startswith("workbench_plugin_lease:", autoescape=True),
            ContentBlock.payload["userId"].as_string() == user_id,
        )
        .all()
    )
    for lease in leases:
        if keys is None or lease.payload.get("applicationKey") in keys:
            lease.payload = {**lease.payload, "closed": True}
    for row in rows:
        # "closing" records keep exact cleanup handles for the janitor.
        row.expires_at = 0
        row.status = "closing"
    if keys is None:
        filters = (PluginResourceTicket.user_id == user_id,)
        asset_filters = (PluginResourceAssetTicket.user_id == user_id,)
    else:
        ids = [row.resource_id for row in rows]
        filters = (
            PluginResourceTicket.user_id == user_id,
            PluginResourceTicket.resource_id.in_(ids),
        )
        asset_filters = (
            PluginResourceAssetTicket.user_id == user_id,
            PluginResourceAssetTicket.resource_id.in_(ids),
        )
    db.execute(delete(PluginResourceTicket).where(*filters))
    db.execute(delete(PluginResourceAssetTicket).where(*asset_filters))


def _external_tickets(db, user_id):
    prefixes = or_(*(ContentBlock.id.startswith(p, autoescape=True) for p in EXTERNAL_PREFIXES))
    return (
        db.query(ContentBlock)
        .filter(prefixes, ContentBlock.payload["subject"].as_string() == user_id)
        .all()
    )


def _revoke_credentials(db, user_id):
    """Revoke every user-owned credential in one transaction; return the live rows."""
    _lock(db, user_id)
    rows = (
        db.query(PluginResource)
        .filter(
            PluginResource.user_id == user_id,
            PluginResource.status.in_(LIVE_STATUSES),
        )
        .order_by(PluginResource.resource_id)
        .all()
    )
    for ticket in _external_tickets(db, user_id):
        db.delete(ticket)
    _revoke_worker_credentials(db, user_id, rows)
    # No external shutdown or independently committing helper before this barrier.
    db.commit()
    for row in rows:
        db.refresh(row)
        db.expunge(row)
    return rows


async def revoke_user_workbench(user_id, session_hash=None):
    """Revoke all user-owned credentials atomically; then attempt every worker.

    Resources and grants are user-owned, not browser-session-owned. Logout
    therefore revokes all matching host tickets, including other tab tickets.
    Third-party sessions remain outside this host boundary.
    """
    return await _close_workers(await run_in_session(_revoke_credentials, user_id))


def stage_unavailable(db, user_id):
    """Revoke stale credentials before attempting each affected worker."""
    from features.workbench import activity
    from features.workbench.plugin_revisions import resolve_revision

    from features.workbench.catalog import directory
    from features.workbench import origin

    origin.current(db)
    _lock(db, user_id)
    projection = directory(db, user_id)
    available = {
        item["applicationKey"]: item
        for item in projection["items"]
        if item["availability"]["available"]
    }
    rows = (
        db.query(PluginResource)
        .filter(
            PluginResource.user_id == user_id,
            PluginResource.application_key.isnot(None),
            PluginResource.status.in_(LIVE_STATUSES),
        )
        .order_by(PluginResource.resource_id)
        .all()
    )
    stale = []
    cleanup_only = []
    for row in rows:
        if row.application_key not in available:
            stale.append(row)
        elif row.status == "closing":
            # A failed ordinary resource close is not an application revocation.
            # Retry only that exact handle; healthy sibling instances keep grants.
            cleanup_only.append(row)
        elif str(available[row.application_key]["revision"]) != str(row.revision):
            try:
                resolve_revision(db, user_id, row.application_key, row.revision, row.instance_id)
            except Exception:
                stale.append(row)
    keys = {row.application_key for row in stale}
    value = activity.read(db, user_id)
    for collection in ("grants", "badges"):
        keys.update(key for key in value.get(collection, {}) if key not in available)
    for collection in ("instances", "background"):
        keys.update(
            record["applicationKey"]
            for record in value.get(collection, {}).values()
            if record["applicationKey"] not in available
        )
    keys.update(
        record["applicationKey"]
        for record in value.get("notifications", [])
        if record["applicationKey"] not in available
    )
    # Approvals without activity/resource records must still be revoked permanently.
    approvals = (
        db.query(ContentBlock)
        .filter(
            ContentBlock.id.startswith("workbench_approval:", autoescape=True),
            ContentBlock.payload["userId"].as_string() == user_id,
        )
        .all()
    )
    keys.update(
        row.payload["applicationKey"]
        for row in approvals
        if row.payload.get("applicationKey") not in available
    )
    # A revoked app grant must not leave a different foreground row executable.
    # Valid retained revisions do not enter keys unless their app is revoked.
    stale = [row for row in rows if row.application_key in keys]
    for ticket in _external_tickets(db, user_id):
        record = ticket.payload
        key = record.get("applicationKey")
        if key not in available or (
            record.get("appRevision") is not None
            and str(record["appRevision"]) != str(available[key]["revision"])
        ):
            db.delete(ticket)
    _revoke_worker_credentials(db, user_id, stale, keys)
    db.commit()
    cleanup = stale + [row for row in cleanup_only if row.application_key not in keys]
    return [row.resource_id for row in cleanup]


def _release_background_reservation(db, row):
    from features.workbench import activity

    _lock(db, row.user_id)
    stored = db.get(ContentBlock, activity.identity(row.user_id))
    if stored is not None:
        value = deepcopy(stored.payload)
        reservation = value.get("background", {}).get(row.resource_id)
        if reservation and reservation.get("revoked"):
            value["background"].pop(row.resource_id)
            stored.payload = value
    db.commit()


async def _close_workers(rows):
    """Stop each worker, then release its reservation; no session spans a shutdown."""
    closed = 0
    failures = []
    for row in rows:
        try:
            await asyncio.wait_for(service.close(row), timeout=10)
            await run_in_session(_release_background_reservation, row)
            closed += 1
        except Exception as exc:
            failures.append(type(exc).__name__)
    if failures:
        logging.getLogger(__name__).warning("Workbench worker cleanup deferred: %s", len(failures))
    # Authorization is already revoked; deferred does not mean OS-process gone.
    return {"closed": closed, "deferred": len(failures)}