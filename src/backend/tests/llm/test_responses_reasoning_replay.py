"""Responses 线的思考回传：上游给什么就原样发回什么。

AgentScope 自带的 Responses formatter 会回发一条 reasoning 条目，但 ``content`` 恒为
空、也从不带 ``encrypted_content`` —— 而这两样正是思考回传真正依赖的东西（对齐
codex：``store: false`` + ``include: ["reasoning.encrypted_content"]``，上游把思考加密
成串交客户端保管，明文 ``reasoning_text`` 同样原样回发）。这些用例钉住「补齐」这件事。
"""

from __future__ import annotations

import pytest
from agentscope.message import Msg, TextBlock, ThinkingBlock, ToolCallBlock

from core.llm.responses_models import (
    REASONING_ITEMS_ATTR,
    ResponsesReplayFormatter,
    WIRE_PROTOCOL,
    _attach_reasoning_items,
)

PROVIDER = "openai_compatible"
MODEL = "test-model"


def _formatter() -> ResponsesReplayFormatter:
    return ResponsesReplayFormatter(
        replay_provider=PROVIDER, replay_model=MODEL, replay_protocol=WIRE_PROTOCOL
    )


def _thinking(text: str, items: list[dict] | None) -> ThinkingBlock:
    block = ThinkingBlock(thinking=text)
    # 跨模型安全标记，生产里由 agentscope_hook_adapter 在最后一帧打上。
    block.provider = PROVIDER
    block.model = MODEL
    block.protocol = WIRE_PROTOCOL
    if items is not None:
        block.reasoning_item_id = items[-1]["id"]
        setattr(block, REASONING_ITEMS_ATTR, items)
    return block


def _turn(thinking: ThinkingBlock) -> Msg:
    return Msg(
        name="assistant",
        content=[
            thinking,
            ToolCallBlock(id="fc_1", name="get_weather", input='{"city": "北京"}'),
        ],
        role="assistant",
    )


async def _rows(msg: Msg) -> list[dict]:
    return await _formatter().format([msg])


@pytest.mark.asyncio
async def test_encrypted_reasoning_is_handed_back_verbatim():
    item = {
        "type": "reasoning",
        "id": "rs_1",
        "summary": [{"type": "summary_text", "text": "先查天气"}],
        "content": [],
        "encrypted_content": "ENCRYPTED-BLOB",
    }
    rows = await _rows(_turn(_thinking("先查天气", [item])))

    reasoning = [r for r in rows if r.get("type") == "reasoning"]
    assert reasoning == [item]
    assert reasoning[0]["encrypted_content"] == "ENCRYPTED-BLOB"


@pytest.mark.asyncio
async def test_plaintext_reasoning_content_survives_the_round_trip():
    """自建 vLLM 不产出加密串，思考正文在 content 里；照样原样回发。"""
    item = {
        "type": "reasoning",
        "id": "rs_2",
        "summary": [],
        "content": [{"type": "reasoning_text", "text": "用户要北京天气，应调用工具"}],
    }
    rows = await _rows(_turn(_thinking("", [item])))

    reasoning = [r for r in rows if r.get("type") == "reasoning"]
    assert reasoning[0]["content"] == item["content"]


@pytest.mark.asyncio
async def test_reasoning_stays_immediately_before_the_call_it_produced():
    item = {"type": "reasoning", "id": "rs_3", "summary": [], "content": []}
    rows = await _rows(_turn(_thinking("想一下", [item])))

    kinds = [r.get("type") for r in rows]
    assert kinds.index("reasoning") == kinds.index("function_call") - 1


@pytest.mark.asyncio
async def test_every_reasoning_item_of_the_turn_is_replayed():
    """一轮里上游给了多条，AgentScope 只留最后一条的 id；不能因此丢掉前面的。"""
    items = [
        {"type": "reasoning", "id": "rs_a", "summary": [], "content": []},
        {"type": "reasoning", "id": "rs_b", "summary": [], "content": []},
    ]
    rows = await _rows(_turn(_thinking("两段思考", items)))

    assert [r["id"] for r in rows if r.get("type") == "reasoning"] == ["rs_a", "rs_b"]


@pytest.mark.asyncio
async def test_upstream_that_gives_nothing_extra_is_left_untouched():
    """没有可回传的原始条目时行为与 AgentScope 原本一致，不凭空造。"""
    plain = _thinking("裸思考", None)
    rows = await _rows(_turn(plain))

    assert not [r for r in rows if r.get("type") == "reasoning"]


@pytest.mark.asyncio
async def test_another_models_reasoning_is_never_replayed_as_native():
    """换模型后旧思考不能伪装成当前模型的推理，否则等于给上游喂假签名。"""
    item = {"type": "reasoning", "id": "rs_x", "summary": [], "content": []}
    foreign = _thinking("别的模型想的", [item])
    foreign.provider = "some-other-vendor"

    rows = await _rows(_turn(foreign))

    assert not [r for r in rows if r.get("type") == "reasoning"]


def test_capture_attaches_items_to_the_turn_thinking_block():
    items = [{"type": "reasoning", "id": "rs_1"}]
    blocks = [ThinkingBlock(thinking="x"), TextBlock(text="y")]

    _attach_reasoning_items(blocks, items)

    assert getattr(blocks[0], REASONING_ITEMS_ATTR) == items


def test_capture_is_a_no_op_when_the_upstream_sent_no_reasoning():
    blocks = [ThinkingBlock(thinking="x")]

    _attach_reasoning_items(blocks, [])

    assert getattr(blocks[0], REASONING_ITEMS_ATTR, None) is None
