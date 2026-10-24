"""多进程下，哪些启动工作只能发生一次（背景见 core/infra/leader.py）。

钉住四条：
* 同一角色只有一个持有者，其余一律落选；
* 持有者失联（租约过期）后，下一个来看的进程接手——否则一个工作进程崩掉就等于
  这些循环永久消失；
* 一次性启动步骤只执行一次，其余进程等它做完再继续；
* 单进程部署不因此产生对 Redis 的新依赖。
"""

from __future__ import annotations

import asyncio

import pytest
from core.infra import leader
from core.infra.ephemeral import LocalEphemeralState


@pytest.fixture()
def shared_store(monkeypatch):
    """一个被所有 "进程" 共用的 TTL keyspace，替代部署里的 Redis。"""
    store = LocalEphemeralState()
    monkeypatch.setattr(leader, "get_ephemeral_state", lambda: store)
    monkeypatch.setattr(leader, "resolved_workers", lambda: 4)
    return store


@pytest.mark.asyncio
async def test_only_one_process_holds_a_role(shared_store):
    first, second = leader.Leadership("backend"), leader.Leadership("backend")

    assert await first._hold() is True
    assert await second._hold() is False
    # 持有者续租不会把自己挤掉
    assert await first._hold() is True


@pytest.mark.asyncio
async def test_a_lapsed_lease_moves_the_role_to_the_next_process(shared_store):
    holder = leader.Leadership("backend", lease_seconds=1)
    successor = leader.Leadership("backend", lease_seconds=1)
    assert await holder._hold() is True
    assert await successor._hold() is False

    await asyncio.sleep(1.05)  # 持有者不再续租 == 进程没了

    assert await successor._hold() is True
    assert await holder._hold() is False


@pytest.mark.asyncio
async def test_release_hands_the_role_over_immediately(shared_store):
    holder = leader.Leadership("backend")
    holder.start(on_elected=_noop, on_deposed=_noop)
    await asyncio.sleep(0)  # 让选举任务跑一轮
    await asyncio.sleep(0.01)
    await holder.stop()

    assert await leader.Leadership("backend")._hold() is True


@pytest.mark.asyncio
async def test_a_boot_step_runs_once_and_the_others_wait_for_it(shared_store):
    ran = []
    released = asyncio.Event()

    async def step():
        ran.append("x")
        await released.wait()

    winner = asyncio.create_task(leader.run_once("seed", step, timeout=5))
    await asyncio.sleep(0.01)
    waiter = asyncio.create_task(leader.run_once("seed", step, timeout=5))
    await asyncio.sleep(0.01)

    assert ran == ["x"], "第二个进程不该重复执行"
    assert not waiter.done(), "第二个进程应当在等第一个做完"

    released.set()
    await asyncio.wait_for(asyncio.gather(winner, waiter), timeout=5)
    assert ran == ["x"]


@pytest.mark.asyncio
async def test_a_single_process_deployment_coordinates_with_nothing(monkeypatch):
    """单进程时不引入任何共享存储依赖——桌面端没有 Redis，Redis 短暂不可用时也要能起。"""
    monkeypatch.setattr(leader, "resolved_workers", lambda: 1)

    def explode():
        raise AssertionError("单进程部署不该去碰共享存储")

    monkeypatch.setattr(leader, "get_ephemeral_state", explode)

    assert await leader.Leadership("backend")._hold() is True
    ran = []
    await leader.run_once("seed", _recorder(ran))
    assert ran == ["x"]


def test_every_startup_step_declares_where_it_belongs():
    """新增启动步骤时必须明确它是每进程一份还是全局一份。

    漏填这一维度的代价不是报错，而是静默地在每个工作进程里各跑一份——调度器把同一个
    任务点燃 N 次这种事，线上是看不出堆栈的。所以让注册表的形状本身成为约束。
    """
    # `from api import app` 拿到的是 FastAPI 实例（api/__init__.py 导出的），不是模块。
    from api.lifecycle import _PER_WORKER, _SINGLETON, _startup_steps

    scopes = {_PER_WORKER, _SINGLETON}
    for entry in _startup_steps():
        assert len(entry) == 5, f"启动步骤登记项应为五元组: {entry}"
        step, stop, gate, roles, scope = entry
        assert callable(step)
        assert stop is None or callable(stop), f"{step.__name__} 的 stop 既不是 None 也不可调用"
        assert isinstance(gate, bool)
        assert roles, f"{step.__name__} 未声明运行角色"
        assert scope in scopes, f"{step.__name__} 的 scope 非法: {scope!r}"


async def _noop() -> None:
    return None


def _recorder(sink):
    async def step() -> None:
        sink.append("x")

    return step

async def test_shutdown_cannot_delete_successor_lease(shared_store):
    holder = leader.Leadership("shutdown")
    holder.start(on_elected=_noop, on_deposed=_noop)
    await asyncio.sleep(0.01)
    await shared_store.drop(holder._key)
    successor = leader.Leadership("shutdown")
    assert await successor._hold()
    await holder.stop()
    assert await shared_store.get(successor._key) == successor._token


async def test_shutdown_stops_work_before_releasing_role(shared_store):
    observed = []
    holder = leader.Leadership("ordered")
    async def stopped():
        observed.append(await shared_store.get(holder._key))
    holder.start(on_elected=_noop, on_deposed=stopped)
    await asyncio.sleep(0.01)
    await holder.stop()
    assert observed == [holder._token]


async def test_failed_gate_cannot_admit_other_workers(shared_store):
    async def fail():
        raise ValueError("required initialization failed")
    with pytest.raises(Exception):
        await leader.run_once("failure", fail)
    with pytest.raises(Exception):
        await leader.run_once("failure", _noop)


async def test_renewal_uncertainty_deposes_work(shared_store, monkeypatch):
    stopped = asyncio.Event()
    holder = leader.Leadership("failure", lease_seconds=1)
    holder.start(on_elected=_noop, on_deposed=_recorder_event(stopped))
    await asyncio.sleep(0.02)
    async def failed(*args, **kwargs):
        raise ConnectionError("Redis lost")
    monkeypatch.setattr(shared_store, "compare_exchange", failed)
    await asyncio.wait_for(stopped.wait(), 1)
    await holder.stop()


def _recorder_event(event):
    async def record():
        event.set()
    return record
