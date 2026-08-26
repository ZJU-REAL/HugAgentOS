"""`model_usage_scope` 必须能在「别的 Context」里安全收尾。

这个作用域是用同步 `@contextmanager` 跨过 async generator 的 `yield` 用的
（见 `agentscope_hook_adapter.on_reply` / `on_reasoning`）。当事件循环的
async-generator 终结器去关闭这种生成器时，`athrow(GeneratorExit)` 跑在**另一个**
`Context` 里，此时 `ContextVar.reset(token)` 会抛 `ValueError`。

后果不是"少记一笔用量"那么轻：`finally` 里第一个 reset 一抛，后面三个全不执行，
整条收尾链断掉——run 永远不落终态，SSE 不发 `[DONE]`，前端的「深度拥抱中…」一直
转，而且前端 `sending` 是全局标志位，连新开的会话都发不出请求。
"""

from __future__ import annotations

import asyncio

import pytest
from core.llm.model_usage import CURRENT_MODEL_USAGE, model_usage_scope


class _Recorder:
    def record_attempt(self, attempt):  # pragma: no cover - 本用例不记账
        return attempt


def _stream():
    """复刻 hook adapter 的形状：同步 with 跨过 async generator 的 yield。"""

    async def _gen():
        with model_usage_scope("run-teardown", _Recorder()):
            yield "first"
            yield "second"

    return _gen()


def test_close_from_another_task_does_not_raise() -> None:
    """事件循环终结器在另一个 Context 里关生成器，不能把收尾链炸断。"""

    async def _main() -> None:
        gen = _stream()
        assert await gen.asend(None) == "first"

        # asyncio.create_task 会复制当前 Context——这正是终结器关闭
        # async generator 时所处的情形，token 就是在"另一个 Context"里被 reset 的。
        await asyncio.create_task(gen.aclose())

    asyncio.run(_main())


def test_scope_is_restored_on_the_normal_path() -> None:
    """正常路径仍必须把 ContextVar 还原，别为了容错把语义放掉。"""

    async def _main() -> None:
        assert CURRENT_MODEL_USAGE.get() is None
        with model_usage_scope("run-normal", _Recorder()):
            context = CURRENT_MODEL_USAGE.get()
            assert context is not None
            assert context.run_id == "run-normal"
        assert CURRENT_MODEL_USAGE.get() is None

    asyncio.run(_main())


def test_inactive_scope_is_a_passthrough() -> None:
    """没有 run_id / recorder 时不该设置任何 ContextVar。"""

    with model_usage_scope("", None):
        assert CURRENT_MODEL_USAGE.get() is None
    with model_usage_scope("run-x", None):
        assert CURRENT_MODEL_USAGE.get() is None


def test_body_exception_still_propagates() -> None:
    """容错只针对 reset，业务异常必须照常抛出。"""

    with pytest.raises(RuntimeError, match="boom"):
        with model_usage_scope("run-err", _Recorder()):
            raise RuntimeError("boom")

    assert CURRENT_MODEL_USAGE.get() is None
