"""Revoke application access before stopping a worker; retain failed cleanup."""

import asyncio

from fastapi import HTTPException

from core.capabilities.plugins.resources import store
from core.capabilities.plugins.resources.owners import runtime_session
from core.db.threads import run_in_session


def _begin(db, resource_id):
    """Revoke the worker's tickets; return a detached row and its target, or None."""
    from core.harness.registry.resource_owners import resource_owner

    row = db.get(store.PluginResource, resource_id)
    if row is None or row.status == "closed":
        return None
    row.status = "closing"
    db.query(store.PluginResourceTicket).filter_by(resource_id=resource_id).delete()
    db.query(store.PluginResourceAssetTicket).filter_by(resource_id=resource_id).delete()
    db.commit()
    try:
        target = store.descriptor(row)
    except HTTPException:
        resource_owner().observe(db, row, "release", "release_failed")
        raise HTTPException(503, "resource_cleanup_credentials_unavailable") from None
    db.expunge(row)
    return row, target


def _finish(db, resource_id, outcome):
    from core.harness.registry.resource_owners import resource_owner

    row = db.get(store.PluginResource, resource_id)
    if row is None:
        return
    if outcome == "ok":
        row.status = "closed"
        db.commit()
    resource_owner().observe(db, row, "release", outcome)


async def close(row):
    """Stop an application worker. Database steps and network waits never overlap."""
    from core.capabilities.plugins.resources.service import request
    from core.sandbox.factory import get_sandbox_provider

    begun = await run_in_session(_begin, row.resource_id)
    if begun is None:
        return
    row, target = begun
    if target.get("process_id"):
        try:
            await asyncio.wait_for(request(row, "/shutdown", {}), 5)
        except (HTTPException, TimeoutError):
            provider = get_sandbox_provider()
            if target.get("provider") != provider.name:
                await run_in_session(_finish, row.resource_id, "release_failed")
                raise HTTPException(503, "resource_cleanup_scope_unavailable")
            result = await provider.write_stdin(
                target["process_id"],
                sandbox_session_id=runtime_session(row),
                user_id=row.user_id,
                chars="\x03",
                yield_time_ms=1000,
            )
            if not result or result.get("exit_code") is None:
                await run_in_session(_finish, row.resource_id, "release_failed")
                raise HTTPException(503, "resource_cleanup_pending")
    await run_in_session(_finish, row.resource_id, "ok")
