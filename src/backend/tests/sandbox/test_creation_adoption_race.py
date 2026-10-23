"""Creation and startup adoption must never deliver the same sandbox twice."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from opensandbox import Sandbox

import core.sandbox.sessions.registry as session_registry
from core.sandbox.errors import SandboxConnectError
from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider
from core.sandbox.providers.opensandbox.volumes import _pool_metadata


def creator():
    p = OpenSandboxProvider.__new__(OpenSandboxProvider)
    p._make_config = lambda: None
    p._session_expiry_s = lambda: 3600
    p._image = "image"
    p._additional_workspace_mounts = AsyncMock(return_value=([], {}))
    p._repoint_execd_direct = AsyncMock()
    return p


def stats():
    return dict.fromkeys(("adopted_jupyter", "adopted_light", "skipped_bound", "killed_invalid", "killed_dead", "killed_overflow"), 0)


def test_adopter_skips_container_while_creator_is_still_delivering_it(monkeypatch):
    a, b = creator(), creator()
    b._pool = SimpleNamespace(_buckets={"light": SimpleNamespace(idle=[], max_idle=2)}, adopt_idle=AsyncMock(return_value=True))
    manager = SimpleNamespace(kill_sandbox=AsyncMock())
    sandbox = SimpleNamespace(id="new", close=AsyncMock(), kill=AsyncMock())
    async def create(*args, **kwargs):
        info = SimpleNamespace(id="new", status=SimpleNamespace(state="Running"), metadata=kwargs["metadata"], entrypoint=kwargs["entrypoint"])
        await b._classify_and_adopt(manager, info, stats())
        return sandbox
    monkeypatch.setattr(Sandbox, "create", create)
    monkeypatch.setattr(Sandbox, "connect", AsyncMock(return_value=sandbox))
    monkeypatch.setattr("core.sandbox.providers.opensandbox.creation._make_skills_volumes", lambda *args: [])
    asyncio.run(a._create_light_sandbox())
    b._pool.adopt_idle.assert_not_awaited()
    manager.kill_sandbox.assert_not_awaited()


@pytest.mark.parametrize("kind", ["light", "jupyter"])
def test_creator_refuses_delivery_when_tracking_ownership_is_elsewhere(monkeypatch, kind):
    a = creator()
    a._track_sandbox = AsyncMock(return_value=False)
    sandbox = SimpleNamespace(id="new")
    monkeypatch.setattr(Sandbox, "create", AsyncMock(return_value=sandbox))
    monkeypatch.setattr("core.sandbox.providers.opensandbox.creation._make_skills_volumes", lambda *args: [])
    with pytest.raises(SandboxConnectError, match="ownership"):
        asyncio.run(a._create_light_sandbox() if kind == "light" else a._create_jupyter_sandbox(user_id="api_ak_test"))


def test_adoption_connect_timeout_preserves_live_container(monkeypatch):
    b = creator()
    manager = SimpleNamespace(kill_sandbox=AsyncMock())
    info = SimpleNamespace(id="live", status=SimpleNamespace(state="Running"), metadata=_pool_metadata("light"), entrypoint=[])
    monkeypatch.setattr(Sandbox, "connect", AsyncMock(side_effect=TimeoutError("busy")))
    asyncio.run(b._classify_and_adopt(manager, info, stats()))
    manager.kill_sandbox.assert_not_awaited()


@pytest.mark.parametrize("state,bound", [("Stopped", False), ("Running", False), ("Stopped", True)])
def test_startup_cleanup_cannot_kill_a_foreign_tracker_or_bound_instance(state, bound):
    b = creator()
    manager = SimpleNamespace(kill_sandbox=AsyncMock())
    # Missing pool-kind enters the invalid-metadata cleanup branch in the Running case.
    info = SimpleNamespace(id="foreign", status=SimpleNamespace(state=state), metadata={"hugagent-owner": "backend"}, entrypoint=[])
    async def scenario():
        if bound:
            await session_registry.remember("active-chat", "foreign")
        else:
            assert await session_registry.track("foreign", "other-worker")
        await b._classify_and_adopt(manager, info, stats())
        manager.kill_sandbox.assert_not_awaited()
    asyncio.run(scenario())


@pytest.mark.parametrize("entrypoint", [["foreign-agent"], ["sleep", "infinity"], ["/opt/opensandbox/code-interpreter.sh"]])
def test_stopped_untagged_client_is_never_deleted(entrypoint):
    b = creator()
    manager = SimpleNamespace(kill_sandbox=AsyncMock())
    info = SimpleNamespace(id="another-client", status=SimpleNamespace(state="Stopped"), metadata={}, entrypoint=entrypoint)
    asyncio.run(b._classify_and_adopt(manager, info, stats()))
    manager.kill_sandbox.assert_not_awaited()
