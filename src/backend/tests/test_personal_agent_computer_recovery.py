"""A permanent computer whose instance is lost or retired must be replaced, never left degraded."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import opensandbox
import pytest

import personal_agent.computer.sandbox as computer
from personal_agent.computer.sandbox import ComputerGone, PersonalSandboxProvider


class Missing(Exception):
    status_code = 404


class Unreachable(Exception):
    status_code = 500


def provider_with(binding):
    provider = object.__new__(PersonalSandboxProvider)
    provider._sessions = {}
    provider._get_session_lock = AsyncMock(return_value=asyncio.Lock())
    provider._binding = Mock(side_effect=binding)
    provider._computer_config = Mock(return_value=(SimpleNamespace(), "token"))
    provider._check_mount_revision = AsyncMock()
    provider._retire = AsyncMock()
    provider._repoint_execd_direct = AsyncMock()
    return provider


@pytest.mark.parametrize("failure", [Missing(), ComputerGone("mounts changed")])
def test_lost_or_retired_instance_is_replaced(monkeypatch, failure):
    old = SimpleNamespace(id="old", close=AsyncMock())
    fresh = SimpleNamespace(id="fresh")
    provider = provider_with([("pa", "old", 1), ("pa", None, 2)])
    provider._provision = AsyncMock(return_value=fresh)
    if isinstance(failure, ComputerGone):
        monkeypatch.setattr(opensandbox.Sandbox, "connect", AsyncMock(return_value=old))
        provider._check_mount_revision.side_effect = failure
    else:
        monkeypatch.setattr(opensandbox.Sandbox, "connect", AsyncMock(side_effect=failure))

    session = asyncio.run(provider._get_or_create_session("box", "owner"))

    assert session.sandbox is fresh
    provider._retire.assert_awaited_once_with("pa", "old")
    provider._provision.assert_awaited_once_with("pa", "box", "owner", 2)
    assert provider._sessions["box"] is session
    if isinstance(failure, ComputerGone):
        old.close.assert_awaited_once()


def test_network_error_keeps_the_binding(monkeypatch):
    provider = provider_with([("pa", "old", 1)])
    provider._provision = AsyncMock()
    monkeypatch.setattr(opensandbox.Sandbox, "connect", AsyncMock(side_effect=Unreachable()))

    with pytest.raises(Unreachable):
        asyncio.run(provider._get_or_create_session("box", "owner"))

    provider._retire.assert_not_awaited()
    provider._provision.assert_not_awaited()


def test_cached_instance_with_changed_mounts_is_replaced(monkeypatch):
    cached = SimpleNamespace(sandbox=SimpleNamespace(id="old", close=AsyncMock()))
    fresh = SimpleNamespace(id="fresh")
    provider = provider_with([("pa", "old", 1), ("pa", None, 2)])
    provider._sessions["box"] = cached
    provider._check_mount_revision.side_effect = ComputerGone("mounts changed")
    provider._provision = AsyncMock(return_value=fresh)
    monkeypatch.setattr(opensandbox.Sandbox, "connect", AsyncMock(side_effect=Missing()))

    session = asyncio.run(provider._get_or_create_session("box", "owner"))

    assert session.sandbox is fresh
    cached.sandbox.close.assert_awaited_once()
    provider._retire.assert_awaited_once_with("pa", "old")


class State:
    async def hold(self, *args, **kwargs): return True
    async def compare_exchange(self, *args, **kwargs): return True


def provisioning(monkeypatch, tmp_path, found_state, generations):
    """A provider whose controller already lists one instance of this generation."""
    found = SimpleNamespace(id="orphan", status=SimpleNamespace(state=found_state))
    manager = SimpleNamespace(
        list_sandbox_infos=AsyncMock(return_value=SimpleNamespace(sandbox_infos=[found])),
        kill_sandbox=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(opensandbox.SandboxManager, "create", AsyncMock(return_value=manager))
    adopted = SimpleNamespace(id="orphan", close=AsyncMock())
    monkeypatch.setattr(opensandbox.Sandbox, "connect", AsyncMock(return_value=adopted))
    monkeypatch.setattr("core.infra.ephemeral.get_ephemeral_state", lambda: State())
    monkeypatch.setattr("personal_agent.workspace.persona.ensure_seeded", lambda ident: None)
    monkeypatch.setattr("core.files.spaces.mounts.provision_volumes", lambda user: ([], {}))
    monkeypatch.setenv("PERSONAL_AGENT_VOLUME_ROOT", str(tmp_path))
    monkeypatch.setenv("PERSONAL_AGENT_LOCAL_VOLUME_ROOT", str(tmp_path))
    row = SimpleNamespace(sandbox_id=None, computer_status="not_provisioned")
    monkeypatch.setattr(computer, "run_in_session", AsyncMock(side_effect=lambda fn: fn(SimpleNamespace(get=lambda *a: row, commit=lambda: None))))

    provider = provider_with([("pa", None, g) for g in generations])
    created = SimpleNamespace(id="fresh")
    admission = SimpleNamespace(create=AsyncMock(return_value=created), release=AsyncMock())
    provider._sandbox_admission = Mock(return_value=admission)
    provider._make_config = Mock(return_value=SimpleNamespace())
    return provider, manager, admission, adopted, row


def test_running_orphan_of_this_generation_is_adopted(monkeypatch, tmp_path):
    provider, manager, admission, adopted, row = provisioning(monkeypatch, tmp_path, "Running", [1])

    sandbox = asyncio.run(provider._provision_locked("pa", "box", "owner", 1))

    assert sandbox is adopted
    assert (row.sandbox_id, row.computer_status) == ("orphan", "ready")
    admission.create.assert_not_awaited()
    provider._retire.assert_not_awaited()


@pytest.mark.parametrize("state", ["Terminated", "Failed"])
def test_stopped_orphan_starts_the_next_generation(monkeypatch, tmp_path, state):
    provider, manager, admission, _, row = provisioning(monkeypatch, tmp_path, state, [1, 2])

    sandbox = asyncio.run(provider._provision_locked("pa", "box", "owner", 1))

    assert sandbox.id == "fresh"
    manager.kill_sandbox.assert_awaited_once_with("orphan")
    provider._retire.assert_awaited_once_with("pa", None)
    assert admission.create.await_args.kwargs["metadata"]["generation"] == "2"
    assert (row.sandbox_id, row.computer_status) == ("fresh", "ready")


@pytest.mark.parametrize("failure, status", [
    (computer.ProvisionInProgress("busy"), "not_provisioned"),
    (Unreachable(), "degraded"),
])
def test_maintenance_marks_degraded_only_on_real_failures(monkeypatch, failure, status):
    import personal_agent.background as background
    row = SimpleNamespace(computer_status="not_provisioned")
    session = SimpleNamespace(execute=lambda *a: SimpleNamespace(all=lambda: [("pa", "box", "owner")]),
                              get=lambda *a: row, commit=lambda: None)
    monkeypatch.setattr(background, "run_in_session", AsyncMock(side_effect=lambda fn: fn(session)))
    provider = SimpleNamespace(ensure_user_workspace=AsyncMock(side_effect=failure), touch_session=AsyncMock())
    monkeypatch.setattr(computer, "get_personal_sandbox_provider", lambda: provider)

    asyncio.run(background.BackgroundServices(None).maintain_computers())

    assert row.computer_status == status
