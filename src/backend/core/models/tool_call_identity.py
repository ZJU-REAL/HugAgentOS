"""工具调用的 id 就是它的全部身份——在模型响应进入系统的那一层把这个身份坐实。

下游全部按 ``tool_call_id`` 认人：工具卡归位、tool_result 配对、上下文 IR 的调用/结果
成对、引用发号、沙箱产物归属。而这个 id 是供应商给的，``ToolCallBlock.id`` 只要求是
``str``——空串照收，同一轮里重复也没人拦。这两种情况下，同一轮的两次并行调用会被当成
同一次：卡片合成一张，结果互相覆盖。

所以在这里补全身份：

- **空 id** → 按「工具名 + 它是本条响应里第几个同名空 id 块」补一个本次模型调用内稳定的
  合成 id。流式增量和最后那条累积响应因此落在同一个 id 上。
- **同一条响应里重复的 id** → 后出现的那个改名。一条响应里出现两次，本身就证明这是两次
  不同的调用，给它们不同的 id 不是猜测，而是把已经确定的事实写下来。

不做任何"配对猜测"。供应商把并行的同名调用全发成空 id 时，增量事件已经无法区分谁是谁，
这里也不去猜——那次调用会带着一个前端没见过的 id 落地，被单独记成一条（后端
``core/chat/tool_log.py`` 与前端 ``utils/toolMatching.ts`` 都是这个规矩），而不是被填进
另一次调用的卡片里。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, List
from uuid import uuid4

from agentscope.model import ChatResponse

logger = logging.getLogger(__name__)


def _field(block: Any, name: str) -> str:
    value = block.get(name) if isinstance(block, dict) else getattr(block, name, None)
    return value if isinstance(value, str) else ""


def _set_id(block: Any, value: str) -> None:
    if isinstance(block, dict):
        block["id"] = value
    else:
        block.id = value


def _tool_calls(response: Any) -> List[Any]:
    return [
        b for b in (getattr(response, "content", None) or []) if _field(b, "type") == "tool_call"
    ]


class ToolCallIdentity:
    """一次模型调用（含它流式吐出的每一条响应）内的 id 修复器。"""

    def __init__(self, label: str) -> None:
        self._label = label
        self._token = uuid4().hex[:8]
        self._synthetic: dict[tuple[str, int], str] = {}

    def apply(self, response: Any) -> None:
        seen: dict[str, int] = {}
        blank: dict[str, int] = {}
        for block in _tool_calls(response):
            name = _field(block, "name")
            call_id = _field(block, "id")
            if not call_id:
                ordinal = blank.get(name, 0)
                blank[name] = ordinal + 1
                call_id = self._synthesize(name, ordinal)
                _set_id(block, call_id)
            repeats = seen.get(call_id, 0)
            seen[call_id] = repeats + 1
            if repeats:
                unique = f"{call_id}-jx{repeats + 1}"
                logger.warning(
                    "%s reused tool call id %r inside one response; this call is now %r",
                    self._label,
                    call_id,
                    unique,
                )
                _set_id(block, unique)
                seen[unique] = seen.get(unique, 0) + 1

    def _synthesize(self, name: str, ordinal: int) -> str:
        key = (name, ordinal)
        call_id = self._synthetic.get(key)
        if call_id is None:
            call_id = f"jx-{self._token}-{len(self._synthetic)}"
            self._synthetic[key] = call_id
            logger.warning(
                "%s returned a tool call without an id (%s); it is now %r",
                self._label,
                name or "<unnamed>",
                call_id,
            )
        return call_id


async def _repaired(
    stream: AsyncGenerator[ChatResponse, None], identity: ToolCallIdentity
) -> AsyncGenerator[ChatResponse, None]:
    async for chunk in stream:
        identity.apply(chunk)
        yield chunk


class ToolCallIdentityMixin:
    """Repair tool-call ids on every response the model produces.

    Must sit before the model class in the MRO
    (``class Foo(ToolCallIdentityMixin, SomeChatModel)``) so ``super()`` is the
    real implementation, and outermost among the mixins so it sees what actually
    leaves the model.
    """

    async def __call__(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        result = await super().__call__(*args, **kwargs)  # type: ignore[misc]
        identity = ToolCallIdentity(
            f"{getattr(self, 'provider_id', '') or type(self).__name__}"
            f"/{getattr(self, 'model', '')}"
        )
        if isinstance(result, ChatResponse):
            identity.apply(result)
            return result
        return _repaired(result, identity)
