import asyncio
import pytest

from core.infra.ephemeral import LocalEphemeralState
from core.sandbox import evaluation_binding as binding
from core.sandbox.errors import SandboxError


@pytest.fixture(autouse=True)
def store(monkeypatch):
    state = LocalEphemeralState()
    monkeypatch.setattr(binding, "get_ephemeral_state", lambda: state)
    return state


async def test_binding_missing_owner_and_expiry_fail_closed(monkeypatch):
    with pytest.raises(SandboxError):
        await binding.get("eval_" + "0" * 32)
    item = await binding.create("alice", "sandbox", 60)
    assert item.chat_id == item.session_id
    with pytest.raises(SandboxError):
        await binding.assert_owner(item.session_id, "bob")
    monkeypatch.setattr(binding.time, "time", lambda: item.expires_at + 1)
    with pytest.raises(SandboxError):
        await binding.get(item.session_id)


async def test_freeze_blocks_new_operations_and_drains_existing():
    item = await binding.create("alice", "sandbox", 60)
    async with binding.operation(item.session_id, "alice"):
        await binding.begin_freeze(item.session_id, "alice")
        with pytest.raises(SandboxError):
            async with binding.operation(item.session_id, "alice"):
                pass
        with pytest.raises(SandboxError):
            await binding.wait_idle(item.session_id, "alice", timeout=0.01)
    await binding.wait_idle(item.session_id, "alice", timeout=1)
    result = await binding.finish_freeze(item.session_id, "alice")
    assert result.phase == "frozen"
    with pytest.raises(SandboxError):
        async with binding.operation(item.session_id, "alice"):
            pass


async def test_concurrent_operation_admission_and_release_do_not_lose_updates():
    item = await binding.create("alice", "sandbox", 60)
    entered, release = asyncio.Queue(), asyncio.Event()

    async def worker():
        async with binding.operation(item.session_id, "alice"):
            await entered.put(True)
            await release.wait()

    tasks = [asyncio.create_task(worker()) for _ in range(12)]
    for _ in tasks:
        await entered.get()
    assert len((await binding.get(item.session_id)).operations) == 12
    release.set()
    await asyncio.gather(*tasks)
    assert not (await binding.get(item.session_id)).operations


async def test_closed_binding_is_a_tombstone_and_cannot_be_recreated():
    item = await binding.create("alice", "sandbox", 60)
    await binding.close(item.session_id, "alice")
    assert (await binding.get(item.session_id)).phase == "closed"
    with pytest.raises(SandboxError):
        await binding.create("alice", "other", 60, lease_id=item.lease_id)


async def test_cancelled_operation_releases_admission():
    item = await binding.create("alice", "sandbox", 60)
    entered = asyncio.Event()
    async def worker():
        async with binding.operation(item.session_id, "alice"):
            entered.set()
            await asyncio.sleep(30)
    task = asyncio.create_task(worker())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (await binding.get(item.session_id)).operations


async def test_concurrent_command_cursor_read_is_rejected():
    item = await binding.create("alice", "sandbox", 60)
    async with binding.operation(item.session_id, "alice", resource="command:a"):
        with pytest.raises(SandboxError, match="already active"):
            async with binding.operation(item.session_id, "alice", resource="command:a"):
                pass
