"""Adversarial lifecycle regressions for shared binding transitions."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.capabilities.plugins.resources import prewarm
import core.sandbox.sessions.registry as session_registry
from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider
from tests.sandbox.test_session_affinity import _worker, _fake_opensandbox, _sandbox_settings


def worker(name):
    stub = _worker(name)
    provider = OpenSandboxProvider.__new__(OpenSandboxProvider)
    provider.__dict__.update(stub.__dict__)
    return provider


def test_cached_worker_cannot_rebind_a_container_reused_by_another_chat(monkeypatch):
    monkeypatch.setattr(prewarm, "schedule", lambda *args: None)
    a, b = worker("a"), worker("b")
    a._jupyter_user_pool = SimpleNamespace(release=AsyncMock())
    a._wipe_workspace = AsyncMock(return_value=True)

    async def scenario():
        original = await a._get_or_create_session("old", "user")
        await b._get_or_create_session("old", "user")
        await a._release_session("old", original, reuse_sandbox=True)
        await session_registry.remember("new", original.sandbox.id)
        restored = await b._get_or_create_session("old", "user")
        assert restored.sandbox.id != original.sandbox.id
        assert await session_registry.lookup("new") == original.sandbox.id
        assert await session_registry.lookup("old") == restored.sandbox.id

    asyncio.run(scenario())


def test_cached_touch_does_not_resurrect_a_retired_binding(monkeypatch):
    monkeypatch.setattr(prewarm, "schedule", lambda *args: None)
    a, b = worker("a"), worker("b")

    async def scenario():
        original = await a._get_or_create_session("old", "user")
        await b._get_or_create_session("old", "user")
        await session_registry.forget("old", original.sandbox.id)
        await session_registry.remember("new", original.sandbox.id)
        assert await b.touch_session("old") is False
        assert await session_registry.lookup("old") is None
        assert await session_registry.lookup("new") == original.sandbox.id

    asyncio.run(scenario())


def test_creation_and_registration_are_one_shared_claim(monkeypatch):
    monkeypatch.setattr(prewarm, "schedule", lambda *args: None)
    a, b = worker("a"), worker("b")
    original_remember = session_registry.remember

    async def scenario():
        entered, resume = asyncio.Event(), asyncio.Event()
        first = True

        async def remember(chat, sandbox):
            nonlocal first
            if first:
                first = False
                entered.set()
                await resume.wait()
            await original_remember(chat, sandbox)

        monkeypatch.setattr(session_registry, "remember", remember)
        task_a = asyncio.create_task(a._get_or_create_session("chat", "user"))
        await entered.wait()
        task_b = asyncio.create_task(b._get_or_create_session("chat", "user"))
        try:
            for _ in range(10):
                await asyncio.sleep(0)
            assert b.created == []
        finally:
            resume.set()
            results = await asyncio.gather(task_a, task_b)
        assert results[0].sandbox.id == results[1].sandbox.id

    asyncio.run(scenario())


def test_queued_snapshot_renewal_keeps_binding_without_resetting_idle(monkeypatch, sandbox_session_store):
    import json
    import time
    import core.sandbox.providers.opensandbox.lifecycle as runtime
    monkeypatch.setattr(runtime.settings.sandbox, 'idle_ttl_s', 60)
    a = worker('a')
    a._parking_in_flight = set()
    a._renew = AsyncMock()
    async def scenario():
        sess = await a._get_or_create_session('chat', 'user')
        sess.last_active_ts = time.monotonic() - 120
        key = session_registry._KEY.format(session_id='chat')
        last_active = time.time() - 120
        await sandbox_session_store.put(key, json.dumps({'sandbox_id': sess.sandbox.id, 'active_at': last_active}), ttl=1)
        await a._protect_queued_snapshots([('chat', sess)])
        await asyncio.sleep(1.05)
        assert await session_registry.lookup('chat') == sess.sandbox.id
        assert not await session_registry.in_use_elsewhere('chat', 60)
    asyncio.run(scenario())
