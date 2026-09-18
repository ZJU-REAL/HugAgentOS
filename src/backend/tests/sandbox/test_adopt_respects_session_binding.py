"""空闲池里只能有无主容器——有主的既不能递给别的会话，也不能当闲置销毁。

回归的是生产事故：一次部署重启后，认领把一台还绑在活跃会话上的容器收进了
``_JupyterUserPool`` 的空闲队列。队列条目的老化时间从入队那一刻起算，而之后一小时
的真实使用只刷新 ``session_registry`` 里的最后使用时间——两套时钟一脱节，整点一到
回收器就按"闲置超 TTL"把会话正在用的容器销毁了，``/workspace`` 里的产物一并没了。

守卫放在 ``acquire`` / ``reap_idle`` 两个**消费点**而不是入队点，是因为容器在队列里
期间还会重新变成有主的：多 worker 下 A 把会话 park 进池时，B 的进程内会话表可能还
缓存着同一台容器，下一轮请求落到 B 就会重新绑定它。认领处那道守卫是另一回事——它决定
的是"不收编之后拿它怎么办"（原样留着，而不是销毁），属于调用方策略。
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from core.sandbox import session_registry
from core.sandbox._opensandbox_internals import _JupyterUserPool, _pool_metadata
from core.sandbox.opensandbox_provider import OpenSandboxProvider


def _sbx(sandbox_id: str):
    return SimpleNamespace(id=sandbox_id)


async def _owner_fn(sbx):
    return await session_registry.bound_session(str(sbx.id))


async def _reap_everything(pool) -> int:
    """让队列里的一切都过期：等一小会儿，再用比它更短的 TTL 回收。

    ``reap_idle`` 对 ``ttl_s <= 0`` 直接短路返回，所以阈值必须是正数。
    """
    await asyncio.sleep(0.02)
    return await pool.reap_idle(ttl_s=0.001)


def _pool(destroyed: list, **kwargs):
    async def factory(user_id):
        return _sbx(f"fresh-{user_id}")

    async def destroy(sbx):
        destroyed.append(sbx.id)

    return _JupyterUserPool(
        factory=factory, destroy_fn=destroy, owner_fn=_owner_fn, **kwargs
    )


# ───── 消费点：空闲队列里的有主容器 ─────


def test_reaping_spares_a_container_a_session_reclaimed():
    """事故本身：容器在池里"闲"过了 TTL，但某个会话已经把它认领回去了。"""
    destroyed: list = []
    pool = _pool(destroyed)

    async def scenario():
        await pool.adopt("u1", _sbx("sbx-live"))
        await session_registry.remember("chat-1", "sbx-live")
        return await _reap_everything(pool)

    assert asyncio.run(scenario()) == 0
    assert destroyed == []


def test_reaping_still_destroys_an_unowned_container():
    """别矫枉过正：真无主的过期容器照旧销毁，否则池子只进不出。"""
    destroyed: list = []
    pool = _pool(destroyed)

    async def scenario():
        await pool.adopt("u1", _sbx("sbx-idle"))
        return await _reap_everything(pool)

    assert asyncio.run(scenario()) == 1
    assert destroyed == ["sbx-idle"]


def test_acquire_does_not_hand_out_a_container_a_session_reclaimed():
    """两个会话共用一台容器就会互相看见对方的 /workspace——宁可新建。"""
    destroyed: list = []
    pool = _pool(destroyed)

    async def scenario():
        await pool.adopt("u1", _sbx("sbx-live"))
        await session_registry.remember("chat-1", "sbx-live")
        return await pool.acquire("u1")

    got = asyncio.run(scenario())

    assert got.id == "fresh-u1"
    # 交还给那个会话，不是销毁——它的生命周期归会话管
    assert destroyed == []


def test_acquire_still_reuses_an_unowned_container():
    """无主容器仍要走热路径复用，这是池子存在的理由。"""
    destroyed: list = []
    pool = _pool(destroyed)

    async def scenario():
        await pool.adopt("u1", _sbx("sbx-idle"))
        return await pool.acquire("u1")

    assert asyncio.run(scenario()).id == "sbx-idle"


# ───── 认领点：重启后不把有主容器收编 ─────


@pytest.fixture
def _fake_opensandbox(monkeypatch):
    """``opensandbox`` SDK 不在测试环境里，认领只用到 ``Sandbox.connect``。"""

    async def connect(sandbox_id, **kwargs):
        return _sbx(sandbox_id)

    monkeypatch.setitem(
        sys.modules, "opensandbox", SimpleNamespace(Sandbox=SimpleNamespace(connect=connect))
    )


def _server_info(sandbox_id: str, user_id: str = "u1"):
    """服务端列出来的一台我们自己的、用户绑定的 jupyter 容器。

    metadata 直接问生产的构造器要，免得以后加了新标签这里还在造过时的形状。
    """
    return SimpleNamespace(
        id=sandbox_id,
        status=SimpleNamespace(state="Running"),
        entrypoint=[],
        metadata=_pool_metadata("jupyter", user_id=user_id),
    )


def _provider(monkeypatch):
    """一个只装了认领这条路所需部件的 provider。"""
    monkeypatch.setattr(
        "core.sandbox.opensandbox_provider._user_bound_sandbox_required", lambda: True
    )
    provider = object.__new__(OpenSandboxProvider)
    provider._make_config = lambda: None
    provider._repoint_execd_direct = AsyncMock(return_value=None)
    provider._jupyter_user_pool = SimpleNamespace(adopt=AsyncMock(return_value=True))
    return provider


def test_adoption_leaves_a_bound_container_to_its_session(monkeypatch, _fake_opensandbox):
    """重启后认领遇到有主容器：既不收编也不销毁，等会话自己 attach 回来。"""
    provider = _provider(monkeypatch)
    mgr = SimpleNamespace(kill_sandbox=AsyncMock())
    stats: dict = defaultdict(int)

    async def scenario():
        await session_registry.remember("chat-1", "sbx-live")
        await provider._classify_and_adopt(mgr, _server_info("sbx-live"), stats)

    asyncio.run(scenario())

    provider._jupyter_user_pool.adopt.assert_not_awaited()
    mgr.kill_sandbox.assert_not_awaited()
    assert stats["skipped_bound"] == 1 and stats["adopted_jupyter"] == 0


def test_adoption_still_reclaims_an_orphan(monkeypatch, _fake_opensandbox):
    """真无主的孤儿照旧收编，重启后的秒级预热不能丢。"""
    provider = _provider(monkeypatch)
    stats: dict = defaultdict(int)

    asyncio.run(
        provider._classify_and_adopt(
            SimpleNamespace(kill_sandbox=AsyncMock()), _server_info("sbx-orphan"), stats
        )
    )

    provider._jupyter_user_pool.adopt.assert_awaited_once()
    assert provider._jupyter_user_pool.adopt.await_args.args[1].id == "sbx-orphan"
    assert stats["adopted_jupyter"] == 1 and stats["skipped_bound"] == 0


# ───── 反向索引本身 ─────


def test_bound_session_answers_who_owns_a_container():
    """登记之后问得出主人，撤销之后问不出。"""

    async def scenario():
        await session_registry.remember("chat-1", "sbx-1")
        owned = await session_registry.bound_session("sbx-1")
        await session_registry.forget("chat-1", "sbx-1")
        return owned, await session_registry.bound_session("sbx-1")

    owned, after_forget = asyncio.run(scenario())

    assert owned == "chat-1"
    assert after_forget is None


def test_a_stale_reverse_entry_is_cleaned_up(sandbox_session_store):
    """会话换了容器：旧容器重新算无主，反向那条顺手清掉，不让两个方向各执一词。"""

    async def scenario():
        await session_registry.remember("chat-1", "sbx-old")
        await session_registry.remember("chat-1", "sbx-new")
        answer = await session_registry.bound_session("sbx-old")
        leftover = await sandbox_session_store.get(
            session_registry._REVERSE_KEY.format(sandbox_id="sbx-old")
        )
        return answer, leftover

    answer, leftover = asyncio.run(scenario())

    assert answer is None
    assert leftover is None
