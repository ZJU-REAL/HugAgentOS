"""Reconcile persisted workers after idle exit, revocation or host restart."""
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
import httpx
from core.db.engine import SessionLocal
from . import store

async def reconcile(db, user_id=None, after="", limit=100):
    from . import service
    query = db.query(store.PluginResource).filter(store.PluginResource.status.in_(["active", "starting", "warming", "warm"]))
    if user_id:
        query = query.filter_by(user_id=user_id)
    rows = query.filter(store.PluginResource.resource_id > after).order_by(store.PluginResource.resource_id).limit(limit).all()
    async def valid(row):
        if row.expires_at <= time.time():
            return False
        if row.status in {"starting", "warming"}:
            return True
        try:
            service.validate_binding(db, row, row.user_id)
            target = store.descriptor(row)
            async with httpx.AsyncClient(timeout=3, trust_env=False, headers=target["headers"]) as client:
                response = await client.get(target["url"] + "/state")
                return response.status_code == 200 and not response.json().get("closed")
        except Exception:
            return False
    results = await asyncio.gather(*(valid(row) for row in rows))
    for row, alive in zip(rows, results):
        if not alive:
            try:
                await service.close(db, row)
            except Exception as exc:
                db.rollback()
                logging.getLogger(__name__).warning("Resource cleanup failed for %s: %s", row.resource_id, type(exc).__name__)
    return rows[-1].resource_id if len(rows) == limit else ""

@asynccontextmanager
async def lifespan(app):
    from . import prewarm
    prewarm.resume()
    async def sweep():
        cursor = ""
        while True:
            await asyncio.sleep(int(os.getenv("PLUGIN_RESOURCE_RECONCILE_SECONDS", "30")))
            try:
                with SessionLocal() as db:
                    cursor = await reconcile(db, after=cursor)
            except Exception as exc:
                logging.getLogger(__name__).warning("Resource reconciliation failed: %s", type(exc).__name__)
    task = asyncio.create_task(sweep())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        from .prewarm import stop
        await stop()
