"""沙箱 provider 的服务循环亲和性回归 —— 对应"一次会话重建 44 个空沙箱"的事故。

事故链条（生产日志实测）：

- provider 是**进程级单例**，平台内置子智能体（explorer/worker/reviewer）**复用主对话
  的 session_id**，为的是看同一份 workspace；但它们跑在 ``_subagent_pool`` 线程里的
  独立事件循环上（``core/llm/subagent_tool.py`` 的 ``asyncio.new_event_loop()``）。
- opensandbox SDK 的 Sandbox / CodeInterpreter、``SandboxPool`` 的锁、共享 httpx client
  都在首次使用时绑定当时的运行循环，被另一个循环碰就抛
  ``is bound to a different event loop``；子智能体线程 ``loop.close()`` 之后，主循环
  再用同一批对象就变成 ``Event loop is closed``。
- 这些异常被判成"沙箱坏了"→ 标记 stale → 下次 acquire 重建一个**全新空沙箱**，
  主智能体先前写在 ``/workspace`` 下的文件随之消失。

所以这里锁的是：provider 上钉了服务循环之后，**跨循环调用必须转发回服务循环执行**，
让 SDK 对象始终只被创建它的那个循环碰。
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from core.sandbox.opensandbox_provider import _on_service_loop


class _FakeProvider:
    """只保留亲和层需要的两样东西：服务循环，和一个绑循环的"SDK 对象"。"""

    def __init__(self):
        self._service_loop = None
        # 模拟 SDK 内部那个绑 loop 的 asyncio.Event。**必须保持 unset**：已 set 的
        # Event 上 wait() 走快路径、压根不碰循环，跨循环也不会抛——那样测试就成了
        # 摆设（同 test_session_lock_cross_loop 里"竞争是复现的关键"那条注释）。
        self._bound_primitive = asyncio.Event()
        self.ran_on = []

    @_on_service_loop
    async def touch_sdk(self):
        # 挂一个真实等待者，让 Event 绑定当前运行循环；换个循环再来就抛
        # "is bound to a different event loop"，与生产日志逐字一致。
        waiter = asyncio.ensure_future(self._bound_primitive.wait())
        await asyncio.sleep(0)
        self._bound_primitive.set()
        await waiter
        self._bound_primitive.clear()
        loop = asyncio.get_running_loop()
        self.ran_on.append(loop)
        return id(loop)


async def _run_in_own_loop(coro_factory):
    """在独立线程的独立事件循环里跑一次，模拟子智能体。

    必须走 ``run_in_executor`` 等它，**不能**同步 ``join()``：转发的前提就是服务
    循环在等待期间照常运转。生产里子智能体正是
    ``await loop.run_in_executor(_subagent_pool, ...)`` 派出去的
    （``core/llm/subagent_tool.py``），同步阻塞服务循环会直接死锁。
    """
    box = {}

    def _thread():
        loop = asyncio.new_event_loop()
        try:
            box["result"] = loop.run_until_complete(coro_factory())
        except BaseException as exc:  # noqa: BLE001 — 要把异常带回主线程断言
            box["error"] = exc
        finally:
            loop.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(pool, _thread), timeout=10
        )
    return box


@pytest.mark.asyncio
async def test_cross_loop_call_is_forwarded_to_service_loop():
    """子智能体循环调用 provider，实际执行必须落在服务循环上。"""
    provider = _FakeProvider()
    provider._service_loop = asyncio.get_running_loop()
    service_loop_id = id(provider._service_loop)

    # 先由服务循环自己碰一次，把"SDK 对象"绑到服务循环上（复现主智能体先建沙箱）。
    assert await provider.touch_sdk() == service_loop_id

    # 再由子智能体的独立循环调用：修好之后应当转发回服务循环，而不是就地执行。
    box = await _run_in_own_loop(provider.touch_sdk)

    assert "error" not in box, f"跨循环调用不该抛：{box.get('error')!r}"
    assert box["result"] == service_loop_id, "跨循环调用没有被转发到服务循环"
    assert all(loop is provider._service_loop for loop in provider.ran_on)


@pytest.mark.asyncio
async def test_service_loop_survives_subagent_loop_close():
    """子智能体循环关闭后，主循环继续用同一个 session 不能受影响。

    这是"重建 44 个沙箱"的直接触发点：子循环 close 掉之后，绑在它上面的连接作废，
    主循环下一次调用就抛 Event loop is closed 并把沙箱判死。
    """
    provider = _FakeProvider()
    provider._service_loop = asyncio.get_running_loop()

    await _run_in_own_loop(provider.touch_sdk)  # 子智能体跑完，它的循环已 close

    # 主循环继续用：必须照常成功，不能因为子循环没了就报错。
    assert await provider.touch_sdk() == id(provider._service_loop)


@pytest.mark.asyncio
async def test_same_loop_call_is_not_forwarded():
    """同循环调用应当直接执行，不绕一圈转发（避免给正常路径加调度开销）。"""
    provider = _FakeProvider()
    provider._service_loop = asyncio.get_running_loop()

    assert await provider.touch_sdk() == id(provider._service_loop)
    assert len(provider.ran_on) == 1


@pytest.mark.asyncio
async def test_without_service_loop_runs_locally():
    """没锚定服务循环时（测试 / 脚本，不走 warmup）退化为就地执行。"""
    provider = _FakeProvider()
    assert provider._service_loop is None

    box = await _run_in_own_loop(provider.touch_sdk)

    assert "error" not in box, f"未锚定时不该抛：{box.get('error')!r}"
    assert box["result"] is not None


@pytest.mark.asyncio
async def test_exception_propagates_back_across_loops():
    """服务循环里抛的异常要原样传回调用方循环，不能被转发层吞掉或改写。"""

    class _Boom(Exception):
        pass

    class _FailingProvider:
        def __init__(self):
            self._service_loop = None

        @_on_service_loop
        async def blow_up(self):
            raise _Boom("sandbox gone")

    provider = _FailingProvider()
    provider._service_loop = asyncio.get_running_loop()

    box = await _run_in_own_loop(provider.blow_up)

    assert isinstance(box.get("error"), _Boom)
    assert str(box["error"]) == "sandbox gone"
