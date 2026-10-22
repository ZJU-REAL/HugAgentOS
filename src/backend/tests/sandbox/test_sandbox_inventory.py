"""Inventory must distinguish ownership, live tracking and missing instances."""
import asyncio
import inspect
from types import SimpleNamespace

import core.sandbox.sessions.registry as session_registry
from core.sandbox.providers.opensandbox.volumes import _METADATA_OWNER_KEY, _METADATA_OWNER_VALUE, _METADATA_POOL_KIND_KEY
from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider


def provider(infos):
    value = OpenSandboxProvider.__new__(OpenSandboxProvider)
    value._sessions = {}
    async def inventory():
        for info in infos:
            yield None, info
    value._iter_server_sandbox_infos = inventory
    return value


def info(sandbox_id):
    return SimpleNamespace(id=sandbox_id, status=SimpleNamespace(state="Running"),
                           metadata={_METADATA_OWNER_KEY: _METADATA_OWNER_VALUE, _METADATA_POOL_KIND_KEY: "light"})


def test_owner_tag_without_a_binding_or_live_tracker_is_an_orphan():
    value = provider([info("orphan")])
    async def scenario():
        result = await value.admin_list_sandboxes()
        assert not result[0].backend_managed
        assert result[0].ownership == "self-orphan"
    asyncio.run(scenario())


def test_shared_worker_tracking_preserves_cross_worker_pool_membership():
    a, b = provider([info("idle")]), provider([info("idle")])
    async def scenario():
        await session_registry.track("idle", "worker-a")
        for value in (a, b):
            result = await value.admin_list_sandboxes()
            assert result[0].backend_managed
            assert result[0].state == "pooled-idle"
        await session_registry.untrack("idle", "worker-a")
        assert not (await b.admin_list_sandboxes())[0].backend_managed
    asyncio.run(scenario())


def test_details_cannot_resurrect_a_controller_missing_instance():
    value = provider([])
    value._sessions = {"chat": SimpleNamespace(sandbox=SimpleNamespace(id="gone"), stale_marked=False)}
    assert asyncio.run(value.admin_get_sandbox("gone")) is None


def test_removed_inventory_flag_has_no_compatibility_parameter():
    assert "include_server" not in inspect.signature(OpenSandboxProvider.admin_list_sandboxes).parameters
