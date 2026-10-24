"""Pool maintenance must not destroy containers adopted by another worker."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.sandbox.sessions.registry as session_registry
from core.sandbox.providers.opensandbox.pool import SandboxPool
from core.sandbox.providers.opensandbox.user_pool import _JupyterUserPool
from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider


def general_pool(owner, liveness=None):
    return SandboxPool(
        jupyter_min_idle=0, light_min_idle=0,
        jupyter_factory=AsyncMock(), light_factory=AsyncMock(),
        destroy_fn=AsyncMock(), owner_fn=owner, liveness_fn=liveness,
    )


def test_sweep_rechecks_ownership_after_the_probe():
    foreign = False

    async def owner(_):
        return "another-worker" if foreign else None

    async def probe(_):
        nonlocal foreign
        foreign = True
        return False

    pool = general_pool(owner, probe)
    pool._buckets["light"].idle.append(SimpleNamespace(id="adopted"))
    asyncio.run(pool.sweep_and_refill())
    assert not pool._buckets["light"].idle
    pool._destroy.assert_not_awaited()


@pytest.mark.parametrize("kind", ["general", "user"])
def test_shutdown_discards_foreign_idle_handles_without_destroying_them(kind):
    owner = AsyncMock(return_value="another-worker")
    box = SimpleNamespace(id="adopted")
    if kind == "general":
        pool = general_pool(owner)
        pool._buckets["light"].idle.append(box)
    else:
        pool = _JupyterUserPool(factory=AsyncMock(), destroy_fn=AsyncMock(), owner_fn=owner)
        asyncio.run(pool.adopt("user", box))
    asyncio.run(pool.shutdown())
    pool._destroy.assert_not_awaited()


def test_unrelated_inherited_chat_claim_does_not_authorize_foreign_destruction():
    value = OpenSandboxProvider.__new__(OpenSandboxProvider)
    box = SimpleNamespace(id="adopted", kill=AsyncMock(), close=AsyncMock())

    async def scenario():
        assert await session_registry.track(box.id, "new-worker")
        async with session_registry.claim("unrelated-chat"):
            await value._destroy_sandbox(box)
        box.kill.assert_not_awaited()
        box.close.assert_awaited_once()
        assert await session_registry.tracked_owner(box.id) == "new-worker"

    asyncio.run(scenario())


@pytest.mark.parametrize("replaced", [False, True])
def test_attached_retirement_only_destroys_the_expected_tracking_generation(replaced, sandbox_session_store):
    value = OpenSandboxProvider.__new__(OpenSandboxProvider)
    box = SimpleNamespace(id="attached", kill=AsyncMock(), close=AsyncMock())

    async def scenario():
        assert await session_registry.track(box.id, "original-worker")
        if replaced:
            await sandbox_session_store.drop(session_registry._TRACK_KEY.format(sandbox_id=box.id))
            assert await session_registry.track(box.id, "replacement-worker")
        async with session_registry.claim("retired-chat"):
            await value._destroy_sandbox(box, expected_owner="original-worker")
        if replaced:
            box.kill.assert_not_awaited()
            assert await session_registry.tracked_owner(box.id) == "replacement-worker"
        else:
            box.kill.assert_awaited_once()
            assert await session_registry.tracked_owner(box.id) is None
        box.close.assert_awaited_once()

    asyncio.run(scenario())


@pytest.mark.parametrize("attached", [False, True])
def test_retiring_worker_cannot_remove_the_destruction_lease(attached):
    value = OpenSandboxProvider.__new__(OpenSandboxProvider)

    async def scenario():
        entered, resume = asyncio.Event(), asyncio.Event()

        async def kill():
            entered.set()
            await resume.wait()

        box = SimpleNamespace(id="retiring", kill=kill, close=AsyncMock())
        original_owner = "original-worker" if attached else value._tracking_owner_token()
        assert await session_registry.track(box.id, original_owner)
        options = {"expected_owner": original_owner} if attached else {}
        task = asyncio.create_task(value._destroy_sandbox(box, **options))
        await entered.wait()
        try:
            # The original worker concurrently reconciles its now-detached session.
            await session_registry.untrack(box.id, original_owner)
            assert not await session_registry.track(box.id, "new-worker")
        finally:
            resume.set()
            await task
        assert await session_registry.tracked_owner(box.id) is None

    asyncio.run(scenario())


def test_destruction_guard_renews_long_controller_calls(monkeypatch, sandbox_session_store):
    monkeypatch.setattr(session_registry, "_LOCK_RENEW_INTERVAL_S", 0.001)
    original_hold = sandbox_session_store.compare_exchange

    async def scenario():
        renewed = asyncio.Event()
        renewals = 0

        async def hold(key, expected, token, *, ttl):
            nonlocal renewals
            if key == session_registry._TRACK_KEY.format(sandbox_id="long-kill"):
                renewals += 1
                if renewals >= 2:
                    renewed.set()
            return await original_hold(key, expected, token, ttl=ttl)

        monkeypatch.setattr(sandbox_session_store, "compare_exchange", hold)
        async with session_registry.destruction_guard("long-kill") as allowed:
            assert allowed
            await asyncio.wait_for(renewed.wait(), timeout=1)
        assert await session_registry.tracked_owner("long-kill") is None

    asyncio.run(scenario())


def test_lost_destruction_guard_interrupts_kill_and_preserves_adopter(monkeypatch, sandbox_session_store):
    monkeypatch.setattr(session_registry, "_LOCK_RENEW_INTERVAL_S", 0.001)
    original_hold = sandbox_session_store.compare_exchange

    async def scenario():
        calls = 0
        key = session_registry._TRACK_KEY.format(sandbox_id="lost-kill")

        async def hold(candidate, expected, token, *, ttl):
            nonlocal calls
            if candidate == key:
                calls += 1
                if calls >= 1:
                    await sandbox_session_store.drop(key)
                    await sandbox_session_store.put(key, "adopter", ttl=ttl)
                    return False
            return await original_hold(candidate, expected, token, ttl=ttl)

        monkeypatch.setattr(sandbox_session_store, "compare_exchange", hold)
        with pytest.raises(RuntimeError, match="destruction lease lost"):
            async with session_registry.destruction_guard("lost-kill") as allowed:
                assert allowed
                await asyncio.Event().wait()
        assert await session_registry.tracked_owner("lost-kill") == "adopter"

    asyncio.run(asyncio.wait_for(scenario(), timeout=1))


@pytest.mark.parametrize("failure", ["cancelled", "timeout"])
def test_uncertain_controller_delete_retains_its_bounded_destruction_fence(failure):
    import httpx

    value = OpenSandboxProvider.__new__(OpenSandboxProvider)

    async def scenario():
        entered = asyncio.Event()

        async def kill():
            entered.set()
            if failure == "timeout":
                raise httpx.ReadTimeout("delete may already be running")
            await asyncio.Event().wait()

        box = SimpleNamespace(id="uncertain-delete", kill=kill, close=AsyncMock())
        original_owner = value._tracking_owner_token()
        assert await session_registry.track(box.id, original_owner)
        task = asyncio.create_task(value._destroy_sandbox(box))
        await entered.wait()
        if failure == "cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        retained = await session_registry.tracked_owner(box.id)
        assert retained and retained != original_owner
        assert not await session_registry.track(box.id, "adopter")
        box.close.assert_awaited_once()

    asyncio.run(scenario())
