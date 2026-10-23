"""Session I/O scopes must retain product-specific permission and binding rules."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

import core.sandbox.sessions.registry as session_registry
from core.sandbox.providers.opensandbox.binding import _OpenSandboxBindingMixin
from core.sandbox.processes.cloud import CloudProcesses
from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider
from core.sandbox.protocol import ProcessRequest
from core.files.spaces.sandbox import SpaceSandboxMixin
from personal_agent.computer.sandbox import PersonalSandboxProvider


class SpaceProvider(SpaceSandboxMixin, OpenSandboxProvider):
    pass


def request():
    return ProcessRequest(script_content="true", script_name="check.sh", language="bash", session_id="chat", user_id="owner")


@pytest.mark.parametrize("entry", ["put", "get", "command"])
def test_space_file_and_command_entrypoints_reject_revoked_grants(monkeypatch, entry):
    provider = SpaceProvider.__new__(SpaceProvider)
    provider._service_loop = None
    provider._get_session_lock = AsyncMock(return_value=asyncio.Lock())
    session = SimpleNamespace(sandbox=SimpleNamespace(id="live", files=SimpleNamespace(read_bytes=AsyncMock())), user_id="owner")
    monkeypatch.setattr(_OpenSandboxBindingMixin, "_resolve_bound_session", AsyncMock(return_value=session))
    async def denied(session):
        assert session_registry.claim_active()
        return False
    provider._grants_match = AsyncMock(side_effect=denied)
    provider._destroy_session = AsyncMock()

    async def scenario():
        with pytest.raises(HTTPException) as caught:
            if entry == "put":
                await provider.put_file("chat", "/file", b"data", "owner")
            elif entry == "get":
                await provider.get_file("chat", "/file", "owner")
            else:
                await CloudProcesses(provider)._spawn(request())
        assert caught.value.status_code == 409
        provider._grants_match.assert_awaited_once_with(session)
        provider._destroy_session.assert_awaited_once_with("chat", session, reuse_sandbox=False)
        session.sandbox.files.read_bytes.assert_not_awaited()
    asyncio.run(scenario())


def test_permanent_command_uses_database_selected_computer_without_generic_registry(monkeypatch):
    provider = PersonalSandboxProvider.__new__(PersonalSandboxProvider)
    commands = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(id="cmd")))
    sandbox = SimpleNamespace(id="permanent", commands=commands, is_healthy=AsyncMock(return_value=True))
    session = SimpleNamespace(sandbox=sandbox, user_id="owner")
    provider._sessions = {"chat": session}
    provider._get_session_lock = AsyncMock(return_value=asyncio.Lock())
    provider._binding = Mock(return_value=("computer", "permanent", 1))
    provider._check_mount_revision = AsyncMock()
    provider._sync_inputs = AsyncMock()
    provider._ready_timeout_s = 1
    generic = AsyncMock(side_effect=AssertionError("must not use ordinary sandbox binding"))
    provider._resolve_bound_session = generic
    remember = AsyncMock(side_effect=AssertionError("must not publish permanent computer as an ordinary session"))
    monkeypatch.setattr(session_registry, "remember", remember)
    async def scenario():
        handle = await CloudProcesses(provider)._spawn(request())
        assert handle.commands is commands
        provider._binding.assert_called_once_with("chat", "owner")
        provider._check_mount_revision.assert_awaited_once_with(sandbox, "owner")
        commands.run.assert_awaited_once()
        generic.assert_not_awaited()
        remember.assert_not_awaited()
    asyncio.run(scenario())
