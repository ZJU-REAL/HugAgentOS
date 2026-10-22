"""Lifecycle protection across I/O, acquisition cancellation and worker exit."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.capabilities.plugins.resources import prewarm
import core.sandbox.sessions.registry as session_registry
from core.sandbox.providers.opensandbox.user_pool import _JupyterUserPool
from core.sandbox.providers.opensandbox.pool import SandboxPool
from tests.sandbox.test_multiworker_lifecycle import worker
from tests.sandbox.test_session_affinity import _fake_opensandbox, _sandbox_settings


def test_file_transfer_holds_shared_claim_until_io_finishes(monkeypatch):
    monkeypatch.setattr(prewarm, "schedule", lambda *args: None)
    a, b = worker("a"), worker("b")
    async def scenario():
        entered, resume, retired = asyncio.Event(), asyncio.Event(), asyncio.Event()
        sess = await a._get_or_create_session("chat", "user")
        async def read(path):
            entered.set()
            await resume.wait()
            return b"safe"
        sess.sandbox.files = SimpleNamespace(read_bytes=read)
        read_task = asyncio.create_task(a.get_file("chat", "/file", "user"))
        await entered.wait()
        async def retire():
            async with session_registry.claim("chat"):
                retired.set()
        retire_task = asyncio.create_task(retire())
        for _ in range(10):
            await asyncio.sleep(0)
        assert not retired.is_set()
        resume.set()
        assert await read_task == b"safe"
        await retire_task
        assert retired.is_set()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["user", "general"])
def test_cancelled_acquisition_returns_capacity(kind):
    async def scenario():
        entered = asyncio.Event()
        async def factory(*args):
            entered.set()
            await asyncio.Event().wait()
        if kind == "user":
            pool = _JupyterUserPool(factory=factory, destroy_fn=AsyncMock())
            acquire = lambda: pool.acquire("user")
            count = lambda: pool.stats()["user"]["in_flight"]
        else:
            pool = SandboxPool(jupyter_factory=factory, light_factory=factory,
                               destroy_fn=AsyncMock(), jupyter_min_idle=0, light_min_idle=0)
            acquire = lambda: pool.acquire("jupyter")
            count = lambda: pool.stats()["jupyter"]["in_flight"]
        task = asyncio.create_task(acquire())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert count() == 0
    asyncio.run(scenario())


def test_shutdown_does_not_destroy_a_shared_session(monkeypatch):
    monkeypatch.setattr(prewarm, "schedule", lambda *args: None)
    a = worker("a")
    a._snapshot_workers_shutdown = asyncio.Event()
    a._snapshot_idle_task = a._snapshot_gc_task = None
    a._pool = SimpleNamespace(shutdown=AsyncMock())
    a._jupyter_user_pool = SimpleNamespace(release=AsyncMock(), shutdown=AsyncMock())
    a._http_client = None
    a._destroy_sandbox = AsyncMock()
    async def scenario():
        sess = await a._get_or_create_session("chat", "user")
        await a.shutdown()
        assert await session_registry.lookup("chat") == sess.sandbox.id
        a._destroy_sandbox.assert_not_awaited()
    asyncio.run(scenario())


def test_opensandbox_worker_constructs_without_bypassing_initialization():
    from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider
    provider = OpenSandboxProvider()
    stats = provider.admin_pool_stats()
    assert stats['sessions'] == 0
    assert stats['scope'] == 'worker'
