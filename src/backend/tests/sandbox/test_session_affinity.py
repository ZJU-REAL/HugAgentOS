"""一个会话只能有一个沙箱容器，不管这一轮落在哪个 worker 上。

回归的是多进程化（``WEB_CONCURRENCY``）带进来的事故：会话到容器的绑定只存在
``OpenSandboxProvider._sessions`` 这个进程内字典里，于是相邻两轮被分到不同 worker
时各建各的容器，两个都活着、各有各的 ``/workspace``。用户看到的是"跨轮随机跳"：
上一轮 nohup 起的进程连同 ``run.log`` 一起消失，其实还在另一台容器里跑着。

这里用两个 mixin 实例当两个 worker——它们共享 :mod:`core.sandbox.sessions.registry`，
正如同机的两个 uvicorn 进程共享 Redis。
"""

from __future__ import annotations

from tests.sandbox.provider_test_support import patch_open_settings

import asyncio
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import core.sandbox.providers.opensandbox.models as session_mod_models
import core.sandbox.sessions.registry as session_registry
from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider
from core.sandbox.errors import SandboxNotFoundError


def _settings(**overrides):
    values = {"opensandbox_snapshot_enabled": False, "idle_ttl_s": 3600}
    values.update(overrides)
    return SimpleNamespace(sandbox=SimpleNamespace(**values))


@pytest.fixture(autouse=True)
def _sandbox_settings(monkeypatch):
    patch_open_settings(monkeypatch, _settings())
    monkeypatch.setattr("core.config.settings.settings", _settings(), raising=False)


async def _age(store, session_id, seconds):
    """把这条登记的"最后使用时间"往前拨，模拟闲置。"""
    key = session_registry._KEY.format(session_id=session_id)
    entry = json.loads(await store.get(key))
    entry["active_at"] -= seconds
    await store.put(key, json.dumps(entry), ttl=7200)


class _FakeSandbox:
    """容器句柄。``alive`` 为假时 ``connect`` 会失败，模拟容器已经没了。"""

    registry: dict[str, "_FakeSandbox"] = {}

    def __init__(self, sandbox_id: str):
        self.id = sandbox_id
        self.alive = True
        _FakeSandbox.registry[sandbox_id] = self


@pytest.fixture(autouse=True)
def _fake_opensandbox(monkeypatch):
    """``opensandbox`` SDK 不在测试环境里，注入一个只实现 ``connect`` 的替身。"""
    _FakeSandbox.registry.clear()

    async def connect(sandbox_id, **kwargs):
        sbx = _FakeSandbox.registry.get(sandbox_id)
        if sbx is None or not sbx.alive:
            raise SandboxNotFoundError(f"sandbox {sandbox_id} not found")
        return sbx

    module = SimpleNamespace(Sandbox=SimpleNamespace(connect=connect))
    monkeypatch.setitem(sys.modules, "opensandbox", module)
    yield
    _FakeSandbox.registry.clear()


def _worker(name: str):
    """一个 worker：自己的会话表、自己的池，共享登记。"""
    worker = OpenSandboxProvider.__new__(OpenSandboxProvider)
    worker._sessions = {}
    worker._session_locks = {}
    worker._registry_lock = __import__("threading").Lock()

    async def get_session_lock(session_id):
        key = (id(asyncio.get_running_loop()), session_id)
        lock = worker._session_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            worker._session_locks[key] = lock
        return lock

    worker._get_session_lock = get_session_lock
    worker._maybe_schedule_renew = lambda session_id, sess: None
    worker._make_config = lambda: None
    worker._renew = AsyncMock()
    worker._repoint_execd_direct = AsyncMock(return_value=None)
    worker.created = []

    async def create_session_for(session_id, user_id=None):
        sandbox_id = f"{name}-sbx-{len(worker.created) + 1}"
        worker.created.append(sandbox_id)
        return session_mod_models.SandboxSessionRecord(
            sandbox=_FakeSandbox(sandbox_id),
                user_id=user_id,
            pool_source="user",
            last_renew_ts=time.monotonic(),
            last_active_ts=time.monotonic(),
        )

    worker._create_session_for = create_session_for
    return worker


def test_a_second_worker_attaches_instead_of_building_its_own_container():
    """同一会话的下一轮换了 worker，必须落回同一个容器。"""
    a, b = _worker("a"), _worker("b")

    async def scenario():
        first = await a._get_or_create_session("chat-1", user_id="u1")
        second = await b._get_or_create_session("chat-1", user_id="u1")
        return first, second

    first, second = asyncio.run(scenario())

    assert first.sandbox.id == second.sandbox.id
    assert a.created == ["a-sbx-1"] and b.created == []
    # 接管来的容器不属于本进程任何一个池，销毁时必须直接销毁
    assert second.pool_source == "attached"


def test_many_rounds_bouncing_between_workers_stay_on_one_container():
    """来回跳十轮也只有一个容器——这是"产物散在两台"的直接回归。"""
    a, b = _worker("a"), _worker("b")

    async def scenario():
        ids = []
        for index in range(10):
            worker = a if index % 2 == 0 else b
            sess = await worker._get_or_create_session("chat-1", user_id="u1")
            ids.append(sess.sandbox.id)
        return ids

    ids = asyncio.run(scenario())

    assert len(set(ids)) == 1
    assert len(a.created) + len(b.created) == 1


def test_a_dead_binding_is_replaced_rather_than_reused():
    """登记指向的容器已经没了：新建一个并改登记，不是连上去报错。"""
    a, b = _worker("a"), _worker("b")

    async def scenario():
        first = await a._get_or_create_session("chat-1", user_id="u1")
        _FakeSandbox.registry[first.sandbox.id].alive = False
        second = await b._get_or_create_session("chat-1", user_id="u1")
        return first, second, await session_registry.lookup("chat-1")

    first, second, bound = asyncio.run(scenario())

    assert second.sandbox.id != first.sandbox.id
    assert b.created == ["b-sbx-1"] and bound == second.sandbox.id


def test_destroying_a_session_clears_the_binding(provider_database):
    """容器销毁后登记必须撤掉，否则下一轮会连一个不存在的容器。"""
    a = _worker("a")
    a._jupyter_user_pool = SimpleNamespace(release=AsyncMock())
    a._wipe_workspace = AsyncMock(return_value=True)

    async def scenario():
        sess = await a._get_or_create_session("chat-1", user_id="u1")
        await a._destroy_session("chat-1", sess)
        return await session_registry.lookup("chat-1")

    assert asyncio.run(scenario()) is None


def test_forget_does_not_clear_a_binding_that_moved_on():
    """撤销登记只认自己那一个容器，不能抹掉别人刚建的绑定。"""

    async def scenario():
        await session_registry.remember("chat-1", "sbx-new")
        await session_registry.forget("chat-1", "sbx-old")
        return await session_registry.lookup("chat-1")

    assert asyncio.run(scenario()) == "sbx-new"


def test_a_worker_does_not_reclaim_a_session_another_worker_is_using():
    """空闲回收按全局活跃时间判断：B 刚用过，A 不能把容器快照掉销毁。"""
    a, b = _worker("a"), _worker("b")
    a._take_snapshot = AsyncMock(return_value="snap-1")

    async def scenario():
        await a._get_or_create_session("chat-1", user_id="u1")
        # A 这边看起来闲了很久，但 B 刚刚服务过这个会话
        a._sessions["chat-1"].last_active_ts = time.monotonic() - 7200
        a._parking_in_flight = set()
        await b._get_or_create_session("chat-1", user_id="u1")
        await a._snapshot_idle_sessions_once()
        await asyncio.sleep(0)
        return await session_registry.lookup("chat-1")

    bound = asyncio.run(scenario())

    a._take_snapshot.assert_not_awaited()
    assert bound is not None


def test_a_globally_idle_session_is_still_reclaimed(sandbox_session_store):
    """没人用了就该回收——上一条不能把空闲回收整个关掉。"""
    a = _worker("a")
    a._parking_in_flight = set()
    a._park_session_via_snapshot = AsyncMock()

    async def scenario():
        await a._get_or_create_session("chat-1", user_id="u1")
        a._sessions["chat-1"].last_active_ts = time.monotonic() - 7200
        await _age(sandbox_session_store, "chat-1", 7200)
        await a._snapshot_idle_sessions_once()

    asyncio.run(scenario())

    a._park_session_via_snapshot.assert_called_once()


def test_an_unreadable_registry_stops_reclamation_rather_than_guessing(monkeypatch):
    """登记读不到时按「还在用」处理：拿不准就别销毁别人的容器。"""

    class Unreachable:
        async def get(self, key):
            raise RuntimeError("store unreachable")

    monkeypatch.setattr(session_registry, "get_ephemeral_state", Unreachable)

    assert asyncio.run(session_registry.in_use_elsewhere("chat-1", 3600)) is True


def test_a_readable_registry_still_reports_idle_sessions_as_free(sandbox_session_store):
    """上一条不能把回收整个关掉：读得到且确实闲着就该放行。"""

    async def scenario():
        await session_registry.remember("chat-1", "sbx-1")
        fresh = await session_registry.in_use_elsewhere("chat-1", 3600)
        await _age(sandbox_session_store, "chat-1", 7200)
        idle = await session_registry.in_use_elsewhere("chat-1", 3600)
        return fresh, idle

    assert asyncio.run(scenario()) == (True, False)


def test_attach_renews_the_existing_container_before_returning():
    worker = _worker("renew")
    _FakeSandbox("nearly-expired")
    session = asyncio.run(worker._attach_session("nearly-expired", "u1"))
    worker._renew.assert_awaited_once_with(session)
    assert session.sandbox.id == "nearly-expired"


def test_dead_container_metadata_does_not_prevent_recreation():
    worker = _worker("replacement")
    _FakeSandbox("expired")
    worker._renew.side_effect = SandboxNotFoundError("Sandbox not found")
    async def scenario():
        await session_registry.remember("expired-chat", "expired")
        return await worker._get_or_create_session("expired-chat", user_id="u1")
    session = asyncio.run(scenario())
    assert session.sandbox.id == "replacement-sbx-1"


def test_transient_renewal_failure_does_not_replace_a_healthy_container():
    worker = _worker("transient")
    _FakeSandbox("healthy")
    worker._renew.side_effect = TimeoutError("management API timed out")
    with pytest.raises(TimeoutError):
        asyncio.run(worker._attach_session("healthy", "u1"))
    assert not worker.created
