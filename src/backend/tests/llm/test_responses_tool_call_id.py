"""Responses 线的工具结果配对：按调用 id 认人，不是按条目 id。

Responses 线上一次函数调用带两个互不相同的 id —— 条目 id（``function_call.id``）与
调用 id（``function_call.call_id``），结果回传只认后者。AgentScope 建
``ToolResultBlock`` 时只留了条目 id，格式化又拿它当 ``function_call_output.call_id``
发出去；DeepSeek 官方端点两者恰好不同（条目 id 是 uuid，调用 id 形如 ``call_00_…``），
于是只要模型调了工具，这一轮就被打回
``No tool call found for tool output with call_id …``。这些用例钉住配对。
"""

from __future__ import annotations

import pytest
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock

from core.llm.responses_models import ResponsesReplayFormatter, WIRE_PROTOCOL

ITEM_ID = "c41e042e-2e51-4b7f-9476-d677e46eadc2"
CALL_ID = "call_00_JsdbgnSxi85atlacyJeb5187"


def _formatter() -> ResponsesReplayFormatter:
    return ResponsesReplayFormatter(
        replay_provider="openai_compatible",
        replay_model="test-model",
        replay_protocol=WIRE_PROTOCOL,
    )


def _turn(call_id: str | None) -> list[Msg]:
    extra = {"call_id": call_id} if call_id else {}
    call = ToolCallBlock(id=ITEM_ID, name="atlas_aggregate", input="{}", **extra)
    result = ToolResultBlock(
        id=ITEM_ID,
        name="atlas_aggregate",
        output=[TextBlock(text="ok")],
        state="success",
    )
    return [
        Msg(name="user", role="user", content=[TextBlock(text="继续")]),
        Msg(name="assistant", role="assistant", content=[TextBlock(text="我来查"), call]),
        Msg(name="assistant", role="assistant", content=[result]),
    ]


async def _pairs(call_id: str | None) -> dict[str, str]:
    items = await _formatter().format(_turn(call_id))
    return {
        item["type"]: item.get("call_id")
        for item in items
        if str(item.get("type") or "").startswith("function_call")
    }


@pytest.mark.asyncio
async def test_tool_output_uses_the_provider_call_id() -> None:
    pairs = await _pairs(CALL_ID)
    assert pairs["function_call"] == CALL_ID
    assert pairs["function_call_output"] == CALL_ID


@pytest.mark.asyncio
async def test_history_replay_pairs_on_the_item_id() -> None:
    """历史里的旧块只剩条目 id，两侧退回同一个值，配对依然自洽。"""
    pairs = await _pairs(None)
    assert pairs["function_call"] == ITEM_ID
    assert pairs["function_call_output"] == ITEM_ID

@pytest.mark.asyncio
async def test_message_items_have_explicit_type_without_changing_tool_items() -> None:
    items = await _formatter().format(_turn(CALL_ID))
    messages = [item for item in items if "role" in item and "content" in item]
    assert messages
    assert all(item.get("type") == "message" for item in messages)
    assert [item["content"][0]["type"] for item in messages] == ["input_text", "input_text"]
    assert [item["type"] for item in items if item.get("type", "").startswith("function_call")] == [
        "function_call", "function_call_output",
    ]
