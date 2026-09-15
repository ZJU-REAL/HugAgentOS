"""思考正文必须真的流到界面上。

Responses 有两条思考通道：``reasoning_summary_text`` 是删减过的摘要（OpenAI 返回的
那条），``reasoning_text`` 是思考原文（自建推理服务返回的那条）。AgentScope 的解析器
只认前者。实测自建 deepseekv4-flash-vision 一轮发 144 帧 ``reasoning_text.delta``，
全被丢掉——界面上是一个空思考块，用户看到的现象是「调到思考为高，却根本没有思考」。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agentscope.message import TextBlock, ThinkingBlock

from core.llm.responses_models import (
    REASONING_ITEMS_ATTR,
    _attach_reasoning_items,
    _ReasoningItemCapture,
)


def _event(kind: str, **fields):
    return SimpleNamespace(type=kind, **fields)


async def _drain(events):
    async def source():
        for event in events:
            yield event

    capture = _ReasoningItemCapture(source())
    seen = [item async for item in capture]
    return capture, seen


@pytest.mark.asyncio
async def test_reasoning_text_deltas_reach_the_parser():
    """原文通道的增量必须变成解析器认得的形状，否则思考一个字都到不了界面。"""
    _, seen = await _drain(
        [
            _event("response.reasoning_text.delta", delta="先"),
            _event("response.reasoning_text.delta", delta="想想"),
            _event("response.output_text.delta", delta="答案"),
        ]
    )

    reasoning = [e for e in seen if e.type == "response.reasoning_summary_text.delta"]
    assert [e.delta for e in reasoning] == ["先", "想想"]
    # 原始事件不再重复下发，否则同一段思考会被累积两遍。
    assert not [e for e in seen if e.type == "response.reasoning_text.delta"]


@pytest.mark.asyncio
async def test_other_events_pass_through_untouched():
    events = [
        _event("response.output_text.delta", delta="a"),
        _event("response.output_item.added", item=SimpleNamespace(type="function_call")),
    ]

    _, seen = await _drain(events)

    assert seen == events


@pytest.mark.asyncio
async def test_an_empty_delta_is_not_forwarded_as_thinking():
    _, seen = await _drain([_event("response.reasoning_text.delta", delta="")])

    assert seen == []


@pytest.mark.asyncio
async def test_the_completed_item_is_still_captured_for_replay():
    item = {"type": "reasoning", "id": "rs_1", "content": []}
    done = _event("response.completed", response=SimpleNamespace(output=[item]))

    capture, _ = await _drain([done])

    assert [one.get("id") for one in capture.items] == ["rs_1"]


def test_thinking_is_backfilled_when_the_upstream_sent_no_deltas():
    """只在终态条目里给思考的上游，也不能让界面上空着。"""
    blocks = [ThinkingBlock(thinking=""), TextBlock(text="答案")]
    items = [
        {
            "type": "reasoning",
            "id": "rs_1",
            "content": [{"type": "reasoning_text", "text": "这是思考"}],
        }
    ]

    _attach_reasoning_items(blocks, items)

    assert blocks[0].thinking == "这是思考"
    assert getattr(blocks[0], REASONING_ITEMS_ATTR) == items


def test_streamed_thinking_is_never_overwritten_by_the_backfill():
    """逐字流出来的才是用户看到的那份，回填不能把它顶掉。"""
    blocks = [ThinkingBlock(thinking="逐字流出来的思考")]
    items = [{"type": "reasoning", "id": "rs_1", "content": [{"type": "reasoning_text", "text": "别覆盖我"}]}]

    _attach_reasoning_items(blocks, items)

    assert blocks[0].thinking == "逐字流出来的思考"


def test_the_encrypted_blob_is_never_shown_as_thinking():
    """加密串是给上游回读的，不是给人看的。"""
    blocks = [ThinkingBlock(thinking="")]
    items = [{"type": "reasoning", "id": "rs_1", "content": [], "encrypted_content": "BLOB"}]

    _attach_reasoning_items(blocks, items)

    assert blocks[0].thinking == ""
