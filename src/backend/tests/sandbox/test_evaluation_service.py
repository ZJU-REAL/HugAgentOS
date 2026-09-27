import asyncio
from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from core.config.settings import settings
from core.infra.ephemeral import LocalEphemeralState
from core.sandbox import evaluation_binding as bindings
from core.sandbox.errors import SandboxError
from core.services import evaluation_sandbox_service as service


@pytest.fixture
def sdk(monkeypatch):
    from opensandbox import Sandbox
    monkeypatch.setattr(bindings, "get_ephemeral_state", lambda: store)
    monkeypatch.setattr(
        "core.sandbox.evaluation_process_guard.capture_baseline",
        AsyncMock(return_value={"1": "100"}),
    )
    store = LocalEphemeralState()
    monkeypatch.setattr(service, "settings", SimpleNamespace(sandbox=replace(settings.sandbox, provider="opensandbox")))
    result = SimpleNamespace(exit_code=0, logs=SimpleNamespace(stdout=[], stderr=[]))
    sandbox = SimpleNamespace(
        id="sandbox-one", commands=SimpleNamespace(run=AsyncMock(return_value=result)),
        files=SimpleNamespace(write_file=AsyncMock()), kill=AsyncMock(), close=AsyncMock(),
    )
    create, connect = AsyncMock(return_value=sandbox), AsyncMock(return_value=sandbox)
    monkeypatch.setattr(Sandbox, "create", create)
    monkeypatch.setattr(Sandbox, "connect", connect)
    return sandbox, create, connect


async def test_one_sandbox_with_no_personal_mounts(sdk):
    sandbox, create, connect = sdk
    item = await service.create("owner", image="ageval-pkg:test", attempt_id="attempt")
    assert create.call_count == 1
    assert create.call_args.kwargs["volumes"] is None
    assert create.call_args.kwargs["env"] is None
    await service.upload(item["lease_id"], "owner", "/attempt/workspace/input.bin", b"data")
    await service.execute(item["lease_id"], "owner", argv=["python3", "-c", "print(1)"])
    assert all(call.args[0] == sandbox.id for call in connect.call_args_list)
    assert create.call_count == 1


async def test_foreign_owner_cannot_touch_existing_box(sdk):
    sandbox, create, connect = sdk
    item = await service.create("owner", image="ageval-pkg:test")
    with pytest.raises(SandboxError, match="owner"):
        await service.execute(item["lease_id"], "other", argv=["true"])
    connect.assert_not_called()


async def test_failed_setup_destroys_created_box(sdk):
    sandbox, create, connect = sdk
    sandbox.commands.run.return_value.exit_code = 1
    with pytest.raises(SandboxError, match="workspace"):
        await service.create("owner", image="ageval-pkg:test")
    sandbox.kill.assert_awaited_once()


@pytest.mark.parametrize("failure", [
    SandboxError("uncertain"), RuntimeError("cancel unavailable"), asyncio.CancelledError()
])
async def test_uncertain_lease_can_still_be_destroyed(sdk, monkeypatch, failure):
    sandbox, create, connect = sdk
    item = await service.create("owner", image="ageval-pkg:test")
    await bindings.uncertain(item["chat_id"], "owner")
    monkeypatch.setattr(service, "freeze", AsyncMock(side_effect=failure))
    await service.destroy(item["lease_id"], "owner")
    sandbox.kill.assert_awaited_once()
    assert (await bindings.get(item["chat_id"])).destroyed
    await service.destroy(item["lease_id"], "owner")
    sandbox.kill.assert_awaited_once()
    assert (await bindings.get(item["chat_id"])).phase == "closed"


async def test_closed_lease_still_destroys_its_container(sdk, monkeypatch):
    sandbox, _, _ = sdk
    item = await service.create("owner", image="ageval-pkg:test")
    await bindings.close(item["chat_id"], "owner")
    monkeypatch.setattr(service, "freeze", AsyncMock(side_effect=SandboxError("closed")))
    await service.destroy(item["lease_id"], "owner")
    sandbox.kill.assert_awaited_once()
