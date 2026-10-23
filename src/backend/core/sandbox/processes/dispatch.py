"""Preserve event-loop ownership and request context across execution threads."""

from __future__ import annotations

import asyncio
import contextvars
import functools
from typing import Any, Callable


def _on_service_loop(method: Callable[..., Any]) -> Callable[..., Any]:
    """把碰 SDK / 池 / httpx 的入口钉在 provider 的服务事件循环上执行。

    provider 是进程级单例，但调用方不止一个事件循环：子智能体跑在
    ``_subagent_pool`` 线程里的独立循环（``core/llm/subagents/tool.py`` 的
    ``asyncio.new_event_loop()``），且**平台内置子智能体复用主对话的
    session_id**（explorer/worker/reviewer 要看同一份 workspace），于是拿到的是
    主循环创建的那个 ``_Session``。

    opensandbox SDK 的 Sandbox / CodeInterpreter、``SandboxPool`` 的
    ``asyncio.Lock``、以及共享的 httpx client 都在**首次使用时绑定当时的运行循环**，
    被另一个循环碰就抛 ``is bound to a different event loop``；等子智能体线程
    ``loop.close()`` 之后，主循环再用同一批对象就变成 ``Event loop is closed`` /
    ``incomplete chunked read``。

    事故形态：这些异常被判成"沙箱坏了"→ 标记 stale → 下次 acquire 重建一个**全新
    空沙箱**，主智能体先前写在 ``/workspace`` 下的文件随之消失（``/workspace/myspace``
    因为每次新建都会重新 seed，所以看起来"只有我的空间还在"）；保活 renew 同样失败，
    旧沙箱空闲到期被服务端回收（404 SANDBOX_NOT_FOUND），于是不断重建。生产上观测到
    一次会话重建 44 个沙箱。

    按循环给锁分桶（``_get_session_lock``）只能救 provider 自己的锁，救不了 SDK 对象
    和池——那些必须**始终由同一个循环**来碰。所以这里反过来做：跨循环调用一律转发回
    服务循环，内部代码因此永远运行在它被创建的那个循环上。

    主循环不会因此死锁：子智能体是 ``await loop.run_in_executor(...)`` 派到线程池的，
    等待期间主循环照常转，能处理转发过来的任务。
    """

    @functools.wraps(method)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        loop = self._service_loop
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if loop is not None and loop.is_closed():
            raise RuntimeError("OpenSandbox service event loop is closed")
        if loop is None or loop is current:
            return await method(self, *args, **kwargs)
        return await _forward_to_loop(loop, method(self, *args, **kwargs))

    return wrapper


async def _forward_to_loop(loop: asyncio.AbstractEventLoop, coro: Any) -> Any:
    """在 ``loop`` 上跑 ``coro``，并在调用方循环里等它。

    走 ``call_soon_threadsafe`` + ``loop.create_task(..., context=...)`` 而不是
    ``run_coroutine_threadsafe``，是为了把调用方的 ContextVar 快照一起带过去
    （``user_id_var`` 等），否则转发之后会丢掉请求级上下文。
    """
    ctx = contextvars.copy_context()
    bridge: asyncio.Future = asyncio.get_running_loop().create_future()

    def _settle(task: asyncio.Task) -> None:
        def _apply() -> None:
            if bridge.done():
                return
            if task.cancelled():
                bridge.cancel()
                return
            exc = task.exception()
            if exc is not None:
                bridge.set_exception(exc)
            else:
                bridge.set_result(task.result())

        bridge.get_loop().call_soon_threadsafe(_apply)

    def _schedule() -> None:
        try:
            loop.create_task(coro, context=ctx).add_done_callback(_settle)
        except BaseException as exc:  # noqa: BLE001 — 服务循环恰好在关停
            coro.close()
            bridge.get_loop().call_soon_threadsafe(bridge.set_exception, exc)

    try:
        loop.call_soon_threadsafe(_schedule)
    except RuntimeError:
        coro.close()
        raise
    return await bridge
