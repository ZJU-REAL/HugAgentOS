"""Generic runner deadline support; installed owners supply lease policy."""
from fastapi import HTTPException
from core.capabilities.plugins.resources import store

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
    from core.harness.registry.resource_owners import resource_owner
    return resource_owner().invalidate(db, row)


async def arm(db, row, *, require_background=False):
    if row.application_key is None or row.chat_id is not None:
        return
    from core.harness.registry.resource_owners import resource_owner
    return await resource_owner().arm(db, row, require_background=require_background)


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
