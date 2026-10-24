"""Responses 思考事件原样交给实际解析器，保留条目身份、分段和增量。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agentscope.message import TextBlock, ThinkingBlock

from core.models.responses import REASONING_ITEMS_ATTR, _attach_reasoning_items, _ReasoningItemCapture


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
async def test_reasoning_events_keep_identity_and_parts_in_real_parser():
    from datetime import datetime
    from agentscope.model import OpenAIResponseModel

    events = [
        _event("response.reasoning_text.delta", item_id="rs_1", content_index=0, delta="先"),
        _event("response.reasoning_text.delta", item_id="rs_1", content_index=0, delta="想想"),
        _event("response.reasoning_text.delta", item_id="rs_1", content_index=1, delta="另一段"),
        _event("response.reasoning_text.delta", item_id="rs_2", content_index=0, delta="另一条"),
        _event("response.reasoning_summary_text.delta", item_id="rs_1", summary_index=0, delta="摘要"),
        _event("response.output_text.delta", item_id="msg_1", content_index=0, delta="答案"),
    ]
    _, seen = await _drain(events)
    assert all(actual is original for actual, original in zip(seen, events))
    assert len(seen) == len(events)

    class Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def __aiter__(self):
            async def source():
                for event in events:
                    yield event
            return source()

    model = object.__new__(OpenAIResponseModel)
    chunks = [
        chunk async for chunk in model._parse_stream_response(
            datetime.now(), _ReasoningItemCapture(Stream())
        )
    ]
    blocks = [block for chunk in chunks for block in chunk.content]
    thinking = [block for block in blocks if isinstance(block, ThinkingBlock)]
    assert [block.thinking for block in thinking] == ["先", "想想", "另一段", "另一条", "摘要"]
    assert thinking[0].id == thinking[1].id
    assert len({block.id for block in thinking}) == 4
    assert [block.text for block in blocks if isinstance(block, TextBlock)] == ["答案"]


@pytest.mark.asyncio
async def test_other_events_pass_through_untouched():
    events = [
        _event("response.output_text.delta", delta="a"),
        _event("response.output_item.added", item=SimpleNamespace(type="function_call")),
    ]

    _, seen = await _drain(events)

    assert seen == events


@pytest.mark.asyncio
async def test_an_empty_delta_is_preserved_for_native_parser():
    _, seen = await _drain([_event("response.reasoning_text.delta", delta="")])

    assert len(seen) == 1
    assert seen[0].delta == ""


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


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupt", [False, True])
async def test_capture_closes_upstream_on_completion_and_cancellation(interrupt):
    import asyncio
    from unittest.mock import AsyncMock
    stream = SimpleNamespace(__aenter__=AsyncMock(), __aexit__=AsyncMock(return_value=False))
    capture = _ReasoningItemCapture(stream)
    if interrupt:
        with pytest.raises(asyncio.CancelledError):
            async with capture as entered:
                assert entered is capture
                raise asyncio.CancelledError()
    else:
        async with capture as entered:
            assert entered is capture
    stream.__aenter__.assert_awaited_once()
    stream.__aexit__.assert_awaited_once()
    assert stream.__aexit__.call_args.args[0] is (asyncio.CancelledError if interrupt else None)
