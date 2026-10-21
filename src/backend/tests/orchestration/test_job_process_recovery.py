"""Durable references must bind to the underlying provider, not its session facade."""

from types import SimpleNamespace

import pytest
from core.sandbox import ProcessRequest
import core.sandbox.providers.opensandbox.recovery as recovery
from core.sandbox.errors import SandboxError
from core.sandbox.sessions.router import SessionSandboxRouter


@pytest.mark.asyncio
async def test_recovery_uses_actual_provider_registry(monkeypatch):
    class Provider:
        name = "opensandbox"
        _service_loop = None

        async def current_sandbox_id(self, session):
            return "sandbox"

        async def _get_or_create_session(self, session, **kwargs):
            return SimpleNamespace(sandbox=SimpleNamespace(id="sandbox", commands=object()))

    ordinary = Provider()
    facade = SessionSandboxRouter(ordinary)
    providers = []

    class Sessions:
        async def start(self, factory, owner, **kwargs):
            handle = await factory()
            assert handle.provider is ordinary
            assert handle.execution_id == "command"
            assert owner == ("chat", "user")
            return {"status": "running", "session_id": "recovered"}

    def service(provider):
        providers.append(provider)
        return SimpleNamespace(sessions=Sessions())

    monkeypatch.setattr(recovery, "process_service", service)
    request = ProcessRequest(
        script_content="", script_name="job.sh", session_id="chat", user_id="user"
    )
    ref = dict(
        provider="opensandbox",
        session_id="chat",
        user_id="user",
        sandbox_id="sandbox",
        execution_id="command",
        cursor=0,
    )
    result = await recovery.recover_execution(facade, ref, request)
    assert result["session_id"] == "recovered"
    assert providers == [ordinary]
    with pytest.raises(SandboxError, match="owner mismatch"):
        await recovery.recover_execution(facade, {**ref, "user_id": "other"}, request)
    with pytest.raises(SandboxError, match="binding"):
        await recovery.recover_execution(facade, {**ref, "sandbox_id": "replaced"}, request)


@pytest.mark.asyncio
async def test_detached_handle_cleanup_does_not_interrupt_adopted_command():
    from core.sandbox.processes.cloud import OpenSandboxHandle

    calls = []

    class Commands:
        async def interrupt(self, command):
            calls.append(command)

    handle = OpenSandboxHandle(SimpleNamespace(name="opensandbox"), None, Commands(), "command")
    handle.detached = True
    await handle.close()
    assert calls == []
    handle.detached = False
    await handle.close()
    assert calls == ["command"]
