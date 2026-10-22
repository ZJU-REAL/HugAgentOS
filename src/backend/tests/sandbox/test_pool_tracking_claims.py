"""Pool delivery must retain exclusive tracking after an expired worker lease."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core.sandbox.sessions.registry as session_registry
from core.sandbox.providers.opensandbox.pool import SandboxPool
from core.sandbox.providers.opensandbox.user_pool import _JupyterUserPool
from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider


def worker():
    return OpenSandboxProvider.__new__(OpenSandboxProvider)


def test_untracked_pool_handle_is_atomically_claimed_before_delivery():
    a, b = worker(), worker()
    box = SimpleNamespace(id="shared")

    async def scenario():
        assert await a._pooled_sandbox_owner(box) is None
        assert await session_registry.tracked_owner(box.id) == a._tracking_owner_token()
        assert not await b._track_sandbox(box.id)
        assert await a._pooled_sandbox_owner(box) is None

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["general", "user"])
@pytest.mark.parametrize("alive", [True, False])
def test_expired_tracking_during_probe_cannot_deliver_another_workers_container(kind, alive, sandbox_session_store):
    a, b = worker(), worker()
    box, fresh = SimpleNamespace(id="shared"), SimpleNamespace(id="fresh")

    async def scenario():
        entered, resume = asyncio.Event(), asyncio.Event()

        async def probe(_):
            entered.set()
            await resume.wait()
            return alive

        if kind == "general":
            pool = SandboxPool(
                jupyter_min_idle=0, light_min_idle=0,
                jupyter_factory=AsyncMock(return_value=fresh),
                light_factory=AsyncMock(return_value=fresh),
                destroy_fn=AsyncMock(), liveness_fn=probe, owner_fn=a._pooled_sandbox_owner,
            )
            pool._buckets["jupyter"].idle.append(box)
            take = lambda: pool.acquire("jupyter")
        else:
            pool = _JupyterUserPool(
                factory=AsyncMock(return_value=fresh), destroy_fn=AsyncMock(),
                liveness_fn=probe, owner_fn=a._pooled_sandbox_owner,
            )
            await pool.adopt("user", box)
            take = lambda: pool.acquire("user")
        assert await a._track_sandbox(box.id)
        task = asyncio.create_task(take())
        await entered.wait()
        # Expiry lets a newly started worker legitimately adopt the same handle.
        await sandbox_session_store.drop(session_registry._TRACK_KEY.format(sandbox_id=box.id))
        assert await b._track_sandbox(box.id)
        resume.set()
        assert (await task).id == fresh.id
        pool._destroy.assert_not_awaited()

    asyncio.run(scenario())


def test_tracking_is_refreshed_before_waiting_for_shared_session_claim(monkeypatch):
    value = worker()
    value._sessions = {"chat": SimpleNamespace(sandbox=SimpleNamespace(id="bound"))}
    value._get_session_lock = AsyncMock(return_value=asyncio.Lock())
    value._refresh_tracking = AsyncMock()
    value._release_session = AsyncMock()

    @asynccontextmanager
    async def claim(_):
        value._refresh_tracking.assert_awaited()
        yield

    monkeypatch.setattr(session_registry, "claim", claim)
    asyncio.run(value._reconcile_sessions())
    value._release_session.assert_awaited_once()


@pytest.mark.parametrize("case", ["general-owner", "general-dead", "user-owner", "user-dead", "user-closed"])
def test_cancelled_early_return_does_not_release_another_active_reservation(case, monkeypatch):
    kind, branch = case.split("-")
    async def scenario():
        entered, resume = asyncio.Event(), asyncio.Event()
        owner = AsyncMock(return_value="another-worker" if branch == "owner" else None)
        if kind == "general":
            pool = SandboxPool(
                jupyter_min_idle=0, light_min_idle=0,
                jupyter_factory=AsyncMock(), light_factory=AsyncMock(),
                destroy_fn=AsyncMock(), owner_fn=owner, liveness_fn=AsyncMock(return_value=False),
            )
            pool._buckets["jupyter"].idle.append(SimpleNamespace(id="foreign"))
            pool._buckets["jupyter"].in_flight = 1
            take = lambda: pool.acquire("jupyter")
            remaining = lambda: pool._buckets["jupyter"].in_flight
        else:
            pool = _JupyterUserPool(
                factory=AsyncMock(), destroy_fn=AsyncMock(),
                owner_fn=owner, liveness_fn=AsyncMock(return_value=False),
            )
            if branch == "closed":
                async def create(_):
                    pool._closed = True
                    return SimpleNamespace(id="new")
                pool._factory = create
            else:
                await pool.adopt("user", SimpleNamespace(id="foreign"))
            pool._in_flight["user"] = 1
            take = lambda: pool.acquire("user")
            remaining = lambda: pool._in_flight["user"]
        original = type(pool)._unreserve
        calls = 0

        async def paused(self, argument):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await resume.wait()
            await original(self, argument)

        monkeypatch.setattr(type(pool), "_unreserve", paused)
        task = asyncio.create_task(take())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == 1
        assert remaining() == 1

    asyncio.run(scenario())
