# -*- coding: utf-8 -*-
"""MidTurn compaction: the ReAct step boundary drives the one shared engine.

Covers what the unification is actually for — the step boundary must use the
same trigger ratio, the same replacement shape and the same persisted
checkpoint as every other phase, instead of AgentScope's separate compressor
whose summary died with the turn.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agentscope.agent import ContextConfig
from agentscope.message import DataBlock, Msg, TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatUsage
from core.llm.context import compaction as C
from core.llm.context.compacting_agent import CompactingAgent, msg_to_history_dict
from core.models.constants import IMAGE_TOKEN_RESERVE
from core.chat.compaction import service as S
from tests.llm.test_compaction_mid_turn import _assistant, _make_agent, _user


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["append_block", "edit_block", "replace_message", "summary"])
async def test_changed_content_can_compact_again_without_more_messages(monkeypatch, change):
    agent = _make_agent([_user("q"), _assistant("answer")])
    calls = []

    async def engine(chat_id, history, **kwargs):
        calls.append(history)
        return [
            {"role": "user", "content": "summary"},
            {"role": "assistant", "content": [{"type": "text", "text": "recent"}]},
        ]

    monkeypatch.setattr(compaction_port, "run_compaction", engine)
    agent.observe_context_tokens(900, 10)
    await agent.compress_context()
    assert len(calls) == 1
    if change == "append_block":
        agent.state.context[-1].content.append(
            ToolResultBlock(id="t1", name="s", output="R" * 50_000)
        )
    elif change == "edit_block":
        agent.state.context[-1].content[0].text += "R" * 50_000
    elif change == "replace_message":
        agent.state.context[-1] = _assistant("R" * 50_000)
    else:
        agent.state.summary = "R" * 50_000
    assert len(agent.state.context) == 2
    agent.observe_context_tokens(900, 10)
    await agent.compress_context()
    assert len(calls) == 2, "content changed even though message count did not"


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_assistant", [False, True])
async def test_sdk_save_anchors_usage_after_response_without_counting_it_twice(existing_assistant):
    context = [_user("q")] + ([_assistant("earlier")] if existing_assistant else [])
    agent = _make_agent(context)
    agent.name = "agent"
    agent.state.reply_id = "reply-1"
    response = [TextBlock(text="R" * 4_000), ToolCallBlock(id="t1", name="s", input="{}")]
    agent._save_to_context(response, ChatUsage(time=0, input_tokens=500, output_tokens=1000))
    assert await agent._measure_context_tokens() == 1500

    agent._save_to_context([ToolResultBlock(id="t1", name="s", output="R" * 50_000)])
    assert await agent._measure_context_tokens() > 14_000

    agent._save_to_context(
        [TextBlock(text="done")], ChatUsage(time=0, input_tokens=16000, output_tokens=10)
    )
    assert (
        await agent._measure_context_tokens() == 16010
    ), "latest request usage, not accumulated Msg usage"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["edit", "replace", "remove", "earlier_append", "summary"])
async def test_rewritten_history_invalidates_the_usage_baseline(change):
    agent = _make_agent([_user("q"), _assistant("answer")])
    agent.observe_context_tokens(500, 20)
    if change == "edit":
        agent.state.context[-1].content[0].text += "new text"
    elif change == "replace":
        agent.state.context[-1] = _assistant("replacement")
    elif change == "remove":
        agent.state.context.pop()
    elif change == "earlier_append":
        agent.state.context[0].content.append(TextBlock(text="new instruction"))
    else:
        agent.state.summary = "new summary"

    measured = await agent._measure_context_tokens()
    assert measured == agent._estimate_prompt_tokens()
    assert agent._jx_observation is None


@pytest.mark.asyncio
async def test_actual_sdk_event_order_does_not_double_count_response(monkeypatch):
    from agentscope.event import ModelCallEndEvent
    from agentscope.model import ChatResponse
    from core.llm.runtime.streaming import StreamingAgent

    agent = _make_agent([_user("q")])
    agent.name = "agent"
    agent.model.model = "test"
    agent.state.reply_id = "reply-1"
    agent.state.pending_model_steps = []

    async def prepare():
        return {}

    async def call(**kwargs):
        async def chunks():
            yield ChatResponse(
                content=[TextBlock(text="R" * 4000)],
                usage=ChatUsage(time=0, input_tokens=500, output_tokens=1000),
                is_last=True,
            )

        return chunks()

    monkeypatch.setattr(agent, "_prepare_model_input", prepare)
    monkeypatch.setattr(agent, "_call_model", call)
    streaming = StreamingAgent(agent, mcp_clients=[])
    seen = False
    async for event in agent._reasoning_impl():
        if isinstance(event, ModelCallEndEvent):
            seen = True
            assert len(agent.state.context) == 1
            _ = [item async for item in streaming._map_event(event)]
            assert agent._jx_observation is None, "response has not entered context yet"
    assert seen
    assert len(agent.state.context) == 2
    assert await agent._measure_context_tokens() == 1500

from core.harness.registry import compaction as compaction_port
