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


async def arm(resource_id, *, require_background=False):
    """Arm the runner deadline of an application resource (no-op for chat resources)."""
    from core.harness.registry.resource_owners import resource_owner
    return await resource_owner().arm(resource_id, require_background=require_background)


def _active_application_ids(db, after, limit):
    return [
        resource_id
        for (resource_id,) in db.query(store.PluginResource.resource_id)
        .filter(
            store.PluginResource.application_key.is_not(None),
            store.PluginResource.status == "active",
            store.PluginResource.resource_id > after,
        )
        .order_by(store.PluginResource.resource_id)
        .limit(limit)
    ]


async def restore(*, after="", limit=100):
    from core.db.threads import run_in_session

    resource_ids = await run_in_session(_active_application_ids, after, limit)
    for resource_id in resource_ids:
        try:
            await arm(resource_id)
        except HTTPException:
            pass  # Existing janitor owns persisted closing cleanup.
    return resource_ids[-1] if len(resource_ids) == limit else ""
