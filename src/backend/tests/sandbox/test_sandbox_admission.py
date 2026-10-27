"""Sandbox admission: shared capacity slots, creation gates and reconciliation.

Every case runs against both ephemeral backends (Redis via fakeredis, and the
in-process map), because workers coordinate only through that store.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import fakeredis.aioredis
import pytest

import core.sandbox.providers.opensandbox.admission as admission_module
from core.infra.ephemeral import LocalEphemeralState, RedisEphemeralState
from core.sandbox.errors import SandboxCapacityExhausted
from core.sandbox.providers.opensandbox.admission import SandboxAdmission, background
from core.sandbox.providers.opensandbox.ledger import (
    ADMISSION_TOKEN_KEY,
    BOUND_TTL_S,
    bound_value,
    orphan_value,
)


@pytest.fixture(params=["local", "redis"])
def store(request, monkeypatch):
    if request.param == "local":
        state = LocalEphemeralState()
    else:
        client = fakeredis.aioredis.FakeRedis(decode_responses=True, protocol=2)
        monkeypatch.setattr("core.infra.ephemeral.get_redis", lambda **_kwargs: client)
        state = RedisEphemeralState()
    monkeypatch.setattr("core.sandbox.providers.opensandbox.admission.get_ephemeral_state",
                        lambda: state)
    monkeypatch.setattr("core.sandbox.providers.opensandbox.ledger.get_ephemeral_state",
                        lambda: state)
    monkeypatch.setattr(admission_module, "CLOCK_SKEW_S", 0)
    return state


class Controller:
    """The controller as admission sees it: an inventory and a create call."""

    def __init__(self):
        self.sandboxes: dict[str, str | None] = {}
        self.running = 0
        self.peak = 0

    async def inventory(self):
        return dict(self.sandboxes)

    def launcher(self, *, delay=0.0, error=None, gate=None):
        async def launch(metadata):
            self.running += 1
            self.peak = max(self.peak, self.running)
            try:
                if gate is not None:
                    await gate.wait()
                await asyncio.sleep(delay)
                if error is not None:
                    raise error
                sandbox_id = uuid.uuid4().hex
                self.sandboxes[sandbox_id] = metadata.get(ADMISSION_TOKEN_KEY)
                return SimpleNamespace(id=sandbox_id)
            finally:
                self.running -= 1
        return launch


def make(controller, *, limit=3, gates=8, wait_s=1.0, headroom=0, grace=60.0):
    admission = SandboxAdmission(
        controller=f"http://controller-{uuid.uuid4().hex}", limit=limit,
        config_factory=lambda: None, create_concurrency=gates, wait_s=wait_s,
        headroom=headroom, ambiguous_grace_s=grace,
    )
    admission._inventory = controller.inventory
    return admission


async def occupied(admission):
    return sorted(slot.sandbox_id or slot.state
                  for _, slot in await admission.ledger.read_all() if slot is not None)


async def test_concurrent_creations_never_exceed_capacity_and_waiters_get_freed_slots(store):
    controller = Controller()
    admission = make(controller, limit=3, wait_s=5)
    release = asyncio.Event()
    first = [asyncio.create_task(admission.create(controller.launcher(gate=release)))
             for _ in range(3)]
    await asyncio.sleep(0.2)
    waiter = asyncio.create_task(admission.create(controller.launcher()))
    await asyncio.sleep(0.3)
    assert controller.peak == 3 and not waiter.done()  # waits instead of failing

    release.set()
    done = await asyncio.gather(*first)
    await admission.release(done[0].id)
    sandbox = await asyncio.wait_for(waiter, 5)

    assert len(await occupied(admission)) == 3
    assert sandbox.id in await occupied(admission)


async def test_overflowing_requests_fail_only_after_waiting_with_capacity_error(store):
    controller = Controller()
    admission = make(controller, limit=2, wait_s=0.3)
    hold = asyncio.Event()
    running = [asyncio.create_task(admission.create(controller.launcher(gate=hold)))
               for _ in range(2)]
    await asyncio.sleep(0.1)
    with pytest.raises(SandboxCapacityExhausted) as failure:
        await admission.create(controller.launcher())
    assert failure.value.limit == 2
    hold.set()
    await asyncio.gather(*running)
    assert controller.peak == 2


async def test_creation_gates_queue_instead_of_refusing(store):
    # A single gate serializes creation; requests queue behind it, none is refused.
    controller = Controller()
    admission = make(controller, limit=5, gates=1, wait_s=5)
    results = await asyncio.gather(*(admission.create(controller.launcher(delay=0.05))
                                     for _ in range(4)))
    assert len({sandbox.id for sandbox in results}) == 4
    assert controller.peak == 1


async def test_prewarm_never_waits_nor_takes_user_headroom(store):
    controller = Controller()
    admission = make(controller, limit=3, headroom=2, wait_s=5)
    with background():
        await admission.create(controller.launcher())
        with pytest.raises(SandboxCapacityExhausted) as failure:
            await admission.create(controller.launcher())
    assert failure.value.background is True
    # Users still get both reserved slots.
    await admission.create(controller.launcher())
    await admission.create(controller.launcher())
    assert len(await occupied(admission)) == 3


async def test_first_admission_accounts_existing_inventory(store):
    controller = Controller()
    controller.sandboxes = {"old-1": None, "old-2": None}
    admission = make(controller, limit=3, wait_s=0.2)
    await admission.create(controller.launcher())
    with pytest.raises(SandboxCapacityExhausted):
        await admission.create(controller.launcher())
    assert {"old-1", "old-2"} <= set(await occupied(admission))


async def test_exhaustion_reconciles_before_failing(store):
    controller = Controller()
    admission = make(controller, limit=2, wait_s=2)
    ledger = admission.ledger
    # Slots of sandboxes the controller already deleted, on a ledger that is
    # otherwise considered ready: only a forced reconciliation can free them.
    for index, sandbox_id in enumerate(["gone-1", "gone-2"]):
        await store.put(ledger.slot_key(index), bound_value(sandbox_id), ttl=BOUND_TTL_S)
    await store.put(ledger.ready_key, "1", ttl=60)
    await asyncio.sleep(0.01)

    sandbox = await admission.create(controller.launcher())

    assert await occupied(admission) == [sandbox.id]


async def test_cancelled_creation_is_held_as_orphan_until_its_outcome_is_known(store):
    controller = Controller()
    admission = make(controller, limit=1, grace=0)
    started = asyncio.Event()

    async def launch(metadata):
        # The controller created it, but the caller gave up before the answer.
        controller.sandboxes["late"] = metadata[ADMISSION_TOKEN_KEY]
        started.set()
        await asyncio.sleep(10)

    task = asyncio.create_task(admission.create(launch))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await occupied(admission) == ["orphan"]

    await admission.reconcile()
    assert await occupied(admission) == ["late"]  # bound to the sandbox it produced


async def test_failed_creation_slot_is_freed_once_the_controller_cannot_finish_it(store):
    controller = Controller()
    admission = make(controller, limit=1, grace=0)
    with pytest.raises(RuntimeError):
        await admission.create(controller.launcher(error=RuntimeError("connection reset")))
    assert await occupied(admission) == ["orphan"]
    await asyncio.sleep(0.01)
    await admission.reconcile()
    assert await occupied(admission) == []


async def test_orphan_within_grace_keeps_its_slot(store):
    controller = Controller()
    admission = make(controller, limit=1, grace=600)
    await store.put(admission.ledger.slot_key(0), orphan_value("t", 600), ttl=BOUND_TTL_S)
    await admission.reconcile()
    assert await occupied(admission) == ["orphan"]


async def test_cancelled_waiter_leaves_no_slot_behind(store):
    controller = Controller()
    admission = make(controller, limit=1, wait_s=5)
    hold = asyncio.Event()
    running = asyncio.create_task(admission.create(controller.launcher(gate=hold)))
    await asyncio.sleep(0.1)
    waiter = asyncio.create_task(admission.create(controller.launcher()))
    await asyncio.sleep(0.2)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    hold.set()
    sandbox = await running
    assert await occupied(admission) == [sandbox.id]


async def test_reconcile_frees_vanished_and_keeps_live_sandboxes(store):
    controller = Controller()
    admission = make(controller, limit=3)
    kept = await admission.create(controller.launcher())
    gone = await admission.create(controller.launcher())
    del controller.sandboxes[gone.id]  # e.g. the controller's expiration timer
    await asyncio.sleep(0.01)
    await admission.reconcile()
    assert await occupied(admission) == [kept.id]


async def test_slot_bound_after_the_inventory_snapshot_survives_reconciliation(store):
    controller = Controller()
    admission = make(controller, limit=2)
    snapshot_taken = asyncio.Event()

    async def inventory():
        snapshot = dict(controller.sandboxes)
        snapshot_taken.set()
        await asyncio.sleep(0.2)
        return snapshot

    admission._inventory = inventory
    await store.put(admission.ledger.ready_key, "1", ttl=60)
    reconciling = asyncio.create_task(admission.reconcile())
    await snapshot_taken.wait()
    sandbox = await admission.create(controller.launcher())
    await reconciling
    assert await occupied(admission) == [sandbox.id]


async def test_unaccounted_sandboxes_reduce_free_capacity(store):
    controller = Controller()
    controller.sandboxes = {"a": None, "b": None, "c": None}
    admission = make(controller, limit=2, wait_s=0.2)
    await admission.reconcile()
    assert await admission.ledger.overflow() == 1
    with pytest.raises(SandboxCapacityExhausted):
        await admission.create(controller.launcher())


async def test_release_frees_the_slot(store):
    controller = Controller()
    admission = make(controller, limit=1, wait_s=0.2)
    sandbox = await admission.create(controller.launcher())
    await admission.release(sandbox.id)
    assert await occupied(admission) == []
    await admission.create(controller.launcher())


async def test_lowered_limit_pulls_high_slots_into_range(store):
    controller = Controller()
    wide = make(controller, limit=4)
    for _ in range(3):
        await wide.create(controller.launcher())
    narrow = SandboxAdmission(
        controller=wide.controller, limit=2, config_factory=lambda: None,
        create_concurrency=2, wait_s=0.2, headroom=0, ambiguous_grace_s=60,
    )
    narrow._inventory = controller.inventory
    await narrow.reconcile()
    low = [slot for index, slot in await narrow.ledger.read_all() if index < 2 and slot]
    assert len(low) == 2
    with pytest.raises(SandboxCapacityExhausted):
        await narrow.create(controller.launcher())


async def test_pool_refill_is_prewarm_and_stops_when_capacity_is_in_use():
    from core.sandbox.providers.opensandbox.pool import SandboxPool

    calls = []

    async def factory():
        calls.append(admission_module._BACKGROUND.get())
        raise SandboxCapacityExhausted(limit=1, background=True)

    async def destroy(_sandbox):
        return None

    pool = SandboxPool(jupyter_min_idle=0, jupyter_max_idle=1, light_min_idle=2, light_max_idle=2,
                       jupyter_factory=factory, light_factory=factory, destroy_fn=destroy)
    await asyncio.wait_for(pool._refill("light"), 1)
    # One background attempt, no retry loop hammering a full deployment.
    assert calls == [True]
    assert pool.stats()["light"]["in_flight"] == 0


async def test_creation_that_never_reached_the_controller_frees_its_slot_at_once(store):
    import httpx
    from opensandbox.exceptions import SandboxApiException

    controller = Controller()
    admission = make(controller, limit=1, grace=600)
    refused = SandboxApiException("connect failed", status_code=0)
    refused.__cause__ = httpx.ConnectError("connection refused")
    with pytest.raises(SandboxApiException):
        await admission.create(controller.launcher(error=refused))
    # A controller outage must not leave capacity held for the request timeout.
    assert await occupied(admission) == []


@pytest.mark.parametrize("status,held_after_reconcile", [(0, True), (500, False)])
async def test_transport_status_zero_is_not_mistaken_for_an_answer(store, status,
                                                                   held_after_reconcile):
    from opensandbox.exceptions import SandboxApiException

    controller = Controller()
    admission = make(controller, limit=1, grace=600)
    with pytest.raises(SandboxApiException):
        await admission.create(controller.launcher(
            error=SandboxApiException("failed", status_code=status)))
    await asyncio.sleep(0.01)
    await admission.reconcile()
    assert (await occupied(admission) == ["orphan"]) is held_after_reconcile


async def test_exhaustion_needs_a_reconciliation_completed_after_the_request(store):
    controller = Controller()
    admission = make(controller, limit=1, wait_s=0.2)
    ledger = admission.ledger
    await store.put(ledger.slot_key(0), bound_value("gone"), ttl=BOUND_TTL_S)
    await store.put(ledger.ready_key, "1", ttl=60)
    # A pass finished a moment before this request: recent, but blind to it.
    await store.put(ledger.fresh_key, repr(__import__("time").time() - 1), ttl=60)
    await asyncio.sleep(0.01)
    sandbox = await admission.create(controller.launcher())
    assert await occupied(admission) == [sandbox.id]


async def test_saturated_creation_gates_report_a_queue_timeout(store):
    controller = Controller()
    admission = make(controller, limit=3, gates=1, wait_s=0.2)
    hold = asyncio.Event()
    running = asyncio.create_task(admission.create(controller.launcher(gate=hold)))
    await asyncio.sleep(0.1)
    with pytest.raises(SandboxCapacityExhausted) as failure:
        await admission.create(controller.launcher())
    assert failure.value.queue_full is True
    hold.set()
    await running
    assert len(await occupied(admission)) == 1  # the timed-out request freed its slot


async def test_stopping_a_lease_does_not_swallow_the_callers_cancellation(store):
    from core.sandbox.providers.opensandbox.admission import _Lease

    admission = make(Controller())

    async def slow_to_stop(self):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.sleep(0.2))
            raise

    async def caller():
        lease = _Lease.__new__(_Lease)
        lease._admission, lease.index, lease.raw, lease.gate = admission, 0, "x", None
        lease._task = asyncio.create_task(slow_to_stop(lease))
        await asyncio.sleep(0)  # let the renewal start, so stopping it takes a while
        await lease.stop()
        return "finished"

    task = asyncio.create_task(caller())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
