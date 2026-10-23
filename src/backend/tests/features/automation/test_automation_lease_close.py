"""Release remains bounded even when renewal suppresses cancellation."""

import asyncio

import pytest
from core.infra import ephemeral
from orchestration.automations import automation_lease as module
from orchestration.automations.automation_scheduler import AutomationScheduler


class ControlledState(ephemeral.LocalEphemeralState):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finish = asyncio.Event()
        self.renewals = 0
        self.deletions = 0

    async def compare_exchange(self, *args, **kwargs):
        self.renewals += 1
        self.entered.set()
        try:
            await self.finish.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.finish.wait()  # deliberately swallow the cancellation
        return await super().compare_exchange(*args, **kwargs)

    async def release(self, *args):
        self.deletions += 1
        return await super().release(*args)


@pytest.fixture
def state(monkeypatch):
    value = ControlledState()
    monkeypatch.setattr(ephemeral, "get_ephemeral_state", lambda: value)
    monkeypatch.setattr(module, "RENEW_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(module, "RELEASE_WAIT_SECONDS", 0.01)
    return value


async def bounded(awaitable):
    return await asyncio.wait_for(awaitable, 1)


async def test_release_wakes_sleep_and_is_idempotent(state, monkeypatch):
    monkeypatch.setattr(module, "RENEW_INTERVAL_SECONDS", 3600)
    lease = module.AutomationLease("sleep")
    assert await lease.acquire()
    await bounded(asyncio.gather(lease.release(), lease.release()))
    await lease.release()
    assert state.deletions == 1
    assert state.renewals == 0
    assert lease.renewal.done()
    assert await state.get(lease.key) is None


async def test_swallowed_cancel_cannot_block_release_or_resurrect_lock(state):
    lease = module.AutomationLease("race")
    assert await lease.acquire()
    await bounded(state.entered.wait())
    await bounded(lease.release())
    assert state.cancelled.is_set()
    assert lease._release_task.done()
    assert await state.get(lease.key) is None
    assert await state.hold(lease.key, "new-owner", ttl=60)
    state.finish.set()
    await bounded(asyncio.gather(lease.renewal, return_exceptions=True))
    assert state.renewals == 1
    assert await state.get(lease.key) == "new-owner"


async def test_cancelled_close_caller_does_not_abort_shared_cleanup(state):
    lease = module.AutomationLease("cancel")
    assert await lease.acquire()
    await bounded(state.entered.wait())
    caller = asyncio.create_task(lease.release())
    await bounded(state.cancelled.wait())
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    await bounded(lease.release())
    assert state.deletions == 1
    state.finish.set()
    await bounded(asyncio.gather(lease.renewal, return_exceptions=True))


async def test_finished_execution_stops_renewal_without_release(state):
    lease = module.AutomationLease("finished")
    assert await lease.acquire()

    async def work():
        return "success"

    assert await lease.run(work()) == "success"
    await bounded(asyncio.gather(lease.renewal, return_exceptions=True))
    assert state.renewals == 0
    # No explicit release: the unrenewed key will expire at its original TTL.
    assert await state.get(lease.key) == lease.token
    await lease.release()


async def test_finished_execution_during_cas_stops_another_renewal(state):
    lease = module.AutomationLease("finished-cas")
    assert await lease.acquire()
    await bounded(state.entered.wait())

    async def work():
        return "success"

    assert await lease.run(work()) == "success"
    state.finish.set()
    await bounded(asyncio.gather(lease.renewal, return_exceptions=True))
    assert state.renewals == 1
    await lease.release()


async def test_delete_timeout_does_not_leave_local_running_marker(state, monkeypatch):
    monkeypatch.setattr(module, "RENEW_INTERVAL_SECONDS", 3600)
    finish = asyncio.Event()
    original = state.release

    async def slow_release(*args):
        await finish.wait()
        return await original(*args)

    monkeypatch.setattr(state, "release", slow_release)
    scheduler = AutomationScheduler()
    assert await scheduler._acquire_lock("delete-timeout")
    await bounded(scheduler._release_lock("delete-timeout"))
    assert not scheduler._leases
    finish.set()
    for _ in range(3):
        await asyncio.sleep(0)
    assert await state.get("jx:auto:lock:delete-timeout") is None


async def test_delete_error_does_not_leave_local_running_marker(state, monkeypatch):
    async def fail(*args):
        raise ConnectionError("store unavailable")

    monkeypatch.setattr(state, "release", fail)
    scheduler = AutomationScheduler()
    assert await scheduler._acquire_lock("error")
    await bounded(scheduler._release_lock("error"))
    assert not scheduler._leases


async def test_stop_is_bounded_with_cancellation_swallowing_renewal(state):
    scheduler = AutomationScheduler()
    assert await scheduler._acquire_lock("stop")
    lease = scheduler._leases["stop"]
    await bounded(state.entered.wait())
    await bounded(scheduler.stop())
    assert not scheduler._leases
    assert await state.get(lease.key) is None
    state.finish.set()
    await bounded(asyncio.gather(lease.renewal, return_exceptions=True))


async def test_redis_cas_cannot_recreate_deleted_lock_or_replace_new_owner(monkeypatch):
    import fakeredis.aioredis

    client = fakeredis.aioredis.FakeRedis(decode_responses=True, protocol=2)
    monkeypatch.setattr(ephemeral, "get_redis", lambda: client)
    store = ephemeral.RedisEphemeralState()
    try:
        assert await store.hold("lease", "old", ttl=60)
        assert await store.release("lease", "old")
        assert not await store.compare_exchange("lease", "old", "old", ttl=60)
        assert await store.get("lease") is None
        assert await store.hold("lease", "new", ttl=60)
        assert not await store.compare_exchange("lease", "old", "old", ttl=60)
        assert not await store.release("lease", "old")
        assert await store.get("lease") == "new"
    finally:
        await client.aclose()


async def test_stalled_renewal_stops_execution_even_if_cas_ignores_cancel(state):
    lease = module.AutomationLease("stalled")
    assert await lease.acquire()
    stopped = asyncio.Event()
    async def work():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    with pytest.raises(RuntimeError, match="锁已失效"):
        await bounded(lease.run(work()))
    assert stopped.is_set()
    await bounded(lease.release())
    assert await state.get(lease.key) is None
    assert await state.hold(lease.key, "successor", ttl=60)
    state.finish.set()
    await asyncio.sleep(0.02)
    assert await state.get(lease.key) == "successor"
