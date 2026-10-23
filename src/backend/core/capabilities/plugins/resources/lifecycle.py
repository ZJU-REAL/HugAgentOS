"""Reconcile persisted workers after idle exit, revocation or host restart."""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import httpx
from core.db.threads import run_in_session

from core.capabilities.plugins.resources import store


def _candidates(db, user_id, after, limit):
    query = db.query(store.PluginResource).filter(
        store.PluginResource.status.in_(["active", "starting", "warming", "warm", "closing"])
    )
    if user_id:
        query = query.filter_by(user_id=user_id)
    rows = (
        query.filter(store.PluginResource.resource_id > after)
        .order_by(store.PluginResource.resource_id)
        .limit(limit)
        .all()
    )
    for row in rows:
        db.expunge(row)
    return rows


def _validate_binding(db, resource_id, user_id):
    from core.capabilities.plugins.resources import service

    service.validate_binding(db, db.get(store.PluginResource, resource_id), user_id)


async def reconcile(user_id=None, after="", limit=100):
    """Close persisted workers that are expired, unbound or no longer answering.

    Rows are read, and each binding is checked, on short sessions of their own;
    the liveness probes and shutdowns in between hold no connection.
    """
    from core.capabilities.plugins.resources import service

    rows = await run_in_session(_candidates, user_id, after, limit)

    async def valid(row):
        if row.status == "closing" or row.expires_at <= time.time():
            return False
        if row.status in {"starting", "warming"}:
            return True
        try:
            await run_in_session(_validate_binding, row.resource_id, row.user_id)
            target = store.descriptor(row)
            async with httpx.AsyncClient(
                timeout=3, trust_env=False, headers=target["headers"]
            ) as client:
                response = await client.get(target["url"] + "/state")
                return response.status_code == 200 and not response.json().get("closed")
        except Exception:
            return False

    results = await asyncio.gather(*(valid(row) for row in rows))
    for row, alive in zip(rows, results):
        if not alive:
            try:
                await service.close(row)
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Resource cleanup failed for %s: %s", row.resource_id, type(exc).__name__
                )
    return rows[-1].resource_id if len(rows) == limit else ""


def _collect(db):
    from core.harness.registry.resource_owners import resource_owner

    resource_owner().collect(db)
    db.commit()


@asynccontextmanager
async def lifespan(app):
    from core.capabilities.plugins.resources import prewarm

    from core.capabilities.plugins.resources.deadlines import restore

    cursor = ""
    while True:
        cursor = await restore(after=cursor)
        if not cursor:
            break
    prewarm.resume()

    async def sweep():
        cursor = ""
        owner_cursor = ""
        while True:
            await asyncio.sleep(int(os.getenv("PLUGIN_RESOURCE_RECONCILE_SECONDS", "30")))
            try:
                from core.harness.registry.resource_owners import resource_owner

                owner_cursor = await resource_owner().sweep(owner_cursor)
                cursor = await reconcile(after=cursor)
                await run_in_session(_collect)
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Resource reconciliation failed: %s", type(exc).__name__
                )

    task = asyncio.create_task(sweep())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        from core.capabilities.plugins.resources.prewarm import stop

        await stop()
