"""Revoke application access before stopping a worker; retain failed cleanup."""

import asyncio

from fastapi import HTTPException

from . import store
from .owners import runtime_session


async def close(db, row):
    from .service import request
    from core.applications.diagnostics import observe_resource

    if row.status == "closed":
        return
    row.status = "closing"
    db.query(store.PluginResourceTicket).filter_by(resource_id=row.resource_id).delete()
    db.query(store.PluginResourceAssetTicket).filter_by(resource_id=row.resource_id).delete()
    db.commit()
    try:
        target = store.descriptor(row)
    except HTTPException:
        observe_resource(db, row, "release", "release_failed")
        raise HTTPException(503, "resource_cleanup_credentials_unavailable") from None
    if target.get("process_id"):
        try:
            await asyncio.wait_for(request(row, "/shutdown", {}), 5)
        except (HTTPException, TimeoutError):
            from core.sandbox.factory import get_sandbox_provider

            provider = get_sandbox_provider()
            if target.get("provider") != provider.name:
                observe_resource(db, row, "release", "release_failed")
                raise HTTPException(503, "resource_cleanup_scope_unavailable")
            result = await provider.write_stdin(
                target["process_id"],
                sandbox_session_id=runtime_session(row),
                user_id=row.user_id,
                chars="\x03",
                yield_time_ms=1000,
            )
            if not result or result.get("exit_code") is None:
                observe_resource(db, row, "release", "release_failed")
                raise HTTPException(503, "resource_cleanup_pending")
    row.status = "closed"
    db.commit()
    observe_resource(db, row, "release", "ok")
