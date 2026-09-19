"""进程内的「脱钩后台任务」的唯一归属。

背景：这个后端表达「这件事放后台做」长期只有一种写法——业务代码就地
``asyncio.create_task(...)``，也就是伸手去抓当时碰巧存在的事件循环。这个写法把调用方
的执行环境（在事件循环上，还是在 FastAPI 的线程池里）泄漏进了业务逻辑，于是在路由从
``async def`` 改成 ``def`` 之后成片失效；而各处又各自长出了不同的兜底（丢弃、返回
False、改用 ``asyncio.run``），失效还不报。

所以这里只做两件事，并且**不提供任何兜底**：

- ``spawn`` 必须在事件循环上调用，不在就抛 ``RuntimeError``。拿不到循环意味着调用方
  选错了机制——真正需要跨请求存活的工作属于数据库队列（见 ``_worker_base``），而不是
  一个随请求消失的协程。悄悄降级只会把「派发就没成功」伪装成「任务跑失败了」。
- 持有强引用直到任务结束。``asyncio.create_task`` 的返回值没人持有时，CPython 允许在
  任务跑完前把它回收掉，表现为后台工作随机不执行。
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine, Set

from core.infra.logging import get_logger

logger = get_logger(__name__)

_tasks: Set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
    """把 *coro* 挂到当前事件循环后台执行，返回值不必接收。

    *name* 会出现在任务名和失败日志里，排查时据此定位是谁派发的。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        raise RuntimeError(
            f"spawn({name}) 必须在事件循环上调用；同步路由跑在线程池里，"
            "需要跨请求存活的工作请落库交给队列工人"
        ) from None

    task = loop.create_task(coro, name=name)
    _tasks.add(task)
    task.add_done_callback(_forget)
    return task


def _forget(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("background_task_failed", task=task.get_name(), error=str(exc))
