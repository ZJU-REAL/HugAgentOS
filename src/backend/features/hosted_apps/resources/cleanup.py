"""Revoke application access before stopping a worker; retain failed cleanup."""

import asyncio

from fastapi import HTTPException

from core.capabilities.plugins.resources import store
from core.capabilities.plugins.resources.owners import runtime_session


async def close(db, row):
    from core.capabilities.plugins.resources.service import request
    from core.harness.registry.resource_owners import resource_owner

    if row.status == "closed":
        return
    row.status = "closing"
    db.query(store.PluginResourceTicket).filter_by(resource_id=row.resource_id).delete()
    db.query(store.PluginResourceAssetTicket).filter_by(resource_id=row.resource_id).delete()
    db.commit()
    try:
        target = store.descriptor(row)
    except HTTPException:
        resource_owner().observe(db, row, "release", "release_failed")
        raise HTTPException(503, "resource_cleanup_credentials_unavailable") from None
    if target.get("process_id"):
        try:
            await asyncio.wait_for(request(row, "/shutdown", {}), 5)
        except (HTTPException, TimeoutError):
            from core.sandbox.factory import get_sandbox_provider

            provider = get_sandbox_provider()
            if target.get("provider") != provider.name:
                resource_owner().observe(db, row, "release", "release_failed")
                raise HTTPException(503, "resource_cleanup_scope_unavailable")
            result = await provider.write_stdin(
                target["process_id"],
                sandbox_session_id=runtime_session(row),
                user_id=row.user_id,
                chars="\x03",
                yield_time_ms=1000,
            )
            if not result or result.get("exit_code") is None:
                resource_owner().observe(db, row, "release", "release_failed")
                raise HTTPException(503, "resource_cleanup_pending")
    row.status = "closed"
    db.commit()
    resource_owner().observe(db, row, "release", "ok")
