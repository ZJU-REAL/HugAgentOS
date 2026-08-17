"""沙箱 session 锁的跨事件循环回归 —— 对应一次"批量作业跑一半沙箱被回收"的事故。

事故形态特别隐蔽：

- provider 是**进程级单例**，但调用方不止一个事件循环——子智能体跑在独立线程的独立
  循环里（``core/llm/subagent_tool.py`` 的 ``asyncio.new_event_loop()``）。
- ``asyncio.Lock`` 在**首次 await 时**绑定当时的 running loop。谁先碰 provider，锁就绑给谁。
- 之后另一个循环再用就抛 ``is bound to a different event loop``。而沙箱保活
  ``touch_session`` 的调用方把异常 except 掉了 → **保活静默失效** → 沙箱空闲 600s 被
  idle reaper 回收 → 正在跑的作业连同 workspace 一起消失，作业状态还停在 running。

所以这里锁的是：同一个 provider 实例，被两个不同事件循环取锁，都必须能正常 await。
"""

import asyncio
import threading

import pytest


class _LockOwner:
    """把两个 provider 共有的锁结构抽出来单测，不去拉起真沙箱。

    结构必须与 opensandbox_provider / cube_provider 的实现保持一致：
    注册表用线程锁（临界区无 await，天然跨 loop），session 锁按 (loop, session) 分桶。
    """

    def __init__(self, impl):
        self._session_locks = {}
        self._registry_lock = threading.Lock()
        self._impl = impl

    async def get(self, session_id):
        return await self._impl(self, session_id)


async def _fixed(self, session_id):
    """当前实现：按 (事件循环, session) 分桶。"""
    key = (id(asyncio.get_running_loop()), session_id)
    with self._registry_lock:
        lock = self._session_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[key] = lock
        return lock


async def _buggy(self, session_id):
    """事故版：全进程共用一把，跨 loop 必炸。"""
    lock = self._session_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        self._session_locks[session_id] = lock
    return lock


async def _contend(owner, session_id):
    """制造**竞争**地用一次锁。

    竞争是复现的关键：``asyncio.Lock.acquire()`` 的快路径（没锁上、没等待者）压根不碰
    事件循环，只有走到慢路径要建 future 时才 ``_get_loop()`` 并把 loop 记死。所以线上是
    并发起来之后才炸的——单跑一次永远看不出问题，这也是它藏得久的原因。
    """

    async def _worker(hold):
        lock = await owner.get(session_id)
        async with lock:
            await asyncio.sleep(hold)

    await asyncio.gather(_worker(0.02), _worker(0.0))
    return "ok"


def _run_in_own_loop(owner, session_id, out):
    """在**独立线程的独立事件循环**里用锁 —— 复刻子智能体的执行形态。"""
    loop = asyncio.new_event_loop()
    try:
        out.append(loop.run_until_complete(_contend(owner, session_id)))
    except BaseException as exc:  # noqa: BLE001
        out.append(exc)
    finally:
        loop.close()


def _drive(impl, session_id="sbx_1"):
    """先在 loop A 用一次，再在 loop B 用一次，返回 loop B 的结果。"""
    owner = _LockOwner(impl)

    first: list = []
    t1 = threading.Thread(target=_run_in_own_loop, args=(owner, session_id, first))
    t1.start()
    t1.join(10)
    assert first and first[0] == "ok", f"第一个循环就没跑通: {first}"

    out: list = []
    t2 = threading.Thread(target=_run_in_own_loop, args=(owner, session_id, out))
    t2.start()
    t2.join(10)
    return out[0]


def test_second_loop_can_take_the_lock():
    """修复后：第二个事件循环取同一个 session 的锁必须成功。"""
    assert _drive(_fixed) == "ok"


def test_buggy_shape_reproduces_the_incident():
    """事故版必须复现——否则上面那条测试就是在测空气。"""
    result = _drive(_buggy)
    assert isinstance(result, RuntimeError)
    assert "different event loop" in str(result)


def test_same_loop_still_serializes():
    """按 loop 分桶不能把同循环内的互斥弄丢——那才是这把锁的本职。"""
    owner = _LockOwner(_fixed)
    order: list = []

    async def _worker(tag, hold):
        lock = await owner.get("sbx_1")
        async with lock:
            order.append(f"{tag}-in")
            await asyncio.sleep(hold)
            order.append(f"{tag}-out")

    async def _main():
        await asyncio.gather(_worker("a", 0.05), _worker("b", 0.0))

    asyncio.run(_main())
    # 真互斥的话，一个进出完整后另一个才进；交叉出现即说明锁没生效
    assert order in (
        ["a-in", "a-out", "b-in", "b-out"],
        ["b-in", "b-out", "a-in", "a-out"],
    ), order


@pytest.mark.parametrize(
    "module_name",
    ["core.sandbox.opensandbox_provider", "core.sandbox.cube_provider"],
)
def test_providers_use_thread_lock_for_registry(module_name):
    """两个 provider 都必须用线程锁守注册表 —— 换回 asyncio.Lock 就会重现事故。"""
    import inspect

    # 这两个 provider 并非所有部署形态都带上；缺席时跳过而不是报错。
    mod = pytest.importorskip(module_name)
    src = inspect.getsource(mod)
    assert "self._registry_lock = threading.Lock()" in src, module_name
    assert "self._registry_lock = asyncio.Lock()" not in src, module_name
    # 分桶键必须带上 loop
    assert "id(asyncio.get_running_loop())" in src, module_name
