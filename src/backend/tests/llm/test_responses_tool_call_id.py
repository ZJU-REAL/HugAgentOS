"""Native SDK call ids remain paired across Responses calls and outputs."""

from __future__ import annotations

import pytest
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock

from core.models.responses import ResponsesReplayFormatter, WIRE_PROTOCOL

ITEM_ID = "c41e042e-2e51-4b7f-9476-d677e46eadc2"
CALL_ID = "call_00_JsdbgnSxi85atlacyJeb5187"


def _formatter() -> ResponsesReplayFormatter:
    return ResponsesReplayFormatter(
        replay_provider="openai_compatible",
        replay_model="test-model",
        replay_protocol=WIRE_PROTOCOL,
    )


def _turn(call_id: str | None) -> list[Msg]:
    canonical_id = call_id or ITEM_ID
    call = ToolCallBlock(id=canonical_id, name="atlas_aggregate", input="{}")
    result = ToolResultBlock(
        id=canonical_id,
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
    assert [item["content"][0]["type"] for item in messages] == ["input_text", "output_text"]
    assert [item["type"] for item in items if item.get("type", "").startswith("function_call")] == [
        "function_call", "function_call_output",
    ]
