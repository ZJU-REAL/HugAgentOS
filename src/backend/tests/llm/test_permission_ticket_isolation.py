"""One ticket per dispatch, even when tool calls run concurrently.

A model round can emit several tool calls at once. The ticket is bound to a
``ContextVar`` around dispatch, so if two governed calls were ever driven from
the same task the second one's ``set`` could be observed by the first one's
downstream code. These tests pin the invariant that matters: whatever a tool
body reads back must belong to *its own* call.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from agentscope.message import ToolCallBlock
from agentscope.tool._response import ToolResponse
from core.llm.tool_permissions import (
    CURRENT_PERMISSION_TICKET,
    PermissionRuntime,
    ToolPermissionMiddleware,
    ToolPermissionRegistry,
    ToolPermissionService,
    local_path_tool,
)


def _tool_call(path: str, call_id: str) -> ToolCallBlock:
    return ToolCallBlock(
        id=call_id,
        name="Read",
        input=json.dumps({"file_path": path}, ensure_ascii=False),
    )


def _middleware() -> ToolPermissionMiddleware:
    registry = ToolPermissionRegistry()
    registry.register("Read", local_path_tool("file_path", "read"), source="native")
    return ToolPermissionMiddleware(
        ToolPermissionService(
            registry,
            PermissionRuntime(
                chat_id="chat-1",
                user_id="user-1",
                interactive=True,
                approval_available=True,
            ),
        )
    )


async def _dispatch(middleware, path: str, call_id: str, observed: dict, barrier):
    """Run one governed call, pausing mid-dispatch so both overlap in time."""

    async def handler(**_kwargs):
        # Both calls are inside their handler at the same moment here.
        await barrier.wait()
        ticket = CURRENT_PERMISSION_TICKET.get()
        observed[call_id] = None if ticket is None else ticket.tool_call_id
        yield ToolResponse(id=call_id)

    async for _item in middleware.on_acting(
        None, {"tool_call": _tool_call(path, call_id)}, handler
    ):
        pass


@pytest.mark.asyncio
async def test_concurrent_governed_calls_each_see_their_own_ticket():
    middleware = _middleware()
    observed: dict = {}
    barrier = asyncio.Barrier(2)

    await asyncio.gather(
        _dispatch(middleware, "/workspace/a.txt", "call-a", observed, barrier),
        _dispatch(middleware, "/workspace/b.txt", "call-b", observed, barrier),
    )

    assert observed == {"call-a": "call-a", "call-b": "call-b"}


@pytest.mark.asyncio
async def test_ticket_is_cleared_after_dispatch_completes():
    middleware = _middleware()
    observed: dict = {}
    barrier = asyncio.Barrier(1)

    await _dispatch(middleware, "/workspace/a.txt", "call-a", observed, barrier)

    assert CURRENT_PERMISSION_TICKET.get() is None


@pytest.mark.asyncio
async def test_ticket_is_cleared_even_when_the_tool_raises():
    middleware = _middleware()

    async def failing_handler(**_kwargs):
        raise RuntimeError("tool exploded")
        yield  # pragma: no cover - generator marker

    with pytest.raises(RuntimeError):
        async for _item in middleware.on_acting(
            None, {"tool_call": _tool_call("/workspace/a.txt", "call-a")}, failing_handler
        ):
            pass

    assert CURRENT_PERMISSION_TICKET.get() is None
