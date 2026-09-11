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
from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.model import ChatUsage
from core.llm import compaction as C
from core.llm.compacting_agent import CompactingAgent, msg_to_history_dict
from core.services import compaction_service as S


def _user(text: str) -> Msg:
    return Msg(name="user", content=[TextBlock(type="text", text=text)], role="user")


def _assistant(text: str) -> Msg:
    return Msg(name="agent", content=[TextBlock(type="text", text=text)], role="assistant")


def _make_agent(context, *, window: int = 1000, chat_id: str = "chat-1", offloader=None):
    """A CompactingAgent with only the attributes compress_context touches.

    Built without ``__init__`` on purpose: the real constructor needs a live
    model, toolkit and MCP wiring, none of which the compaction hook reads.
    """
    agent = object.__new__(CompactingAgent)
    agent._jx_observation = None
    agent._jx_compacted_cursor = None
    agent._jx_trigger_ratio = 0.8
    agent.model = SimpleNamespace(context_size=window)
    agent.offloader = offloader
    agent.context_config = ContextConfig()
    agent.state = SimpleNamespace(
        context=list(context), summary="", chat_id=chat_id, session_id="sess-1"
    )
    return agent


# ── Token metering ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_measure_prefers_real_usage_plus_trailing_estimate():
    """Real usage + estimate of what was appended after it (pi/Codex's formula)."""
    agent = _make_agent([_user("a"), _assistant("b")])
    agent.observe_context_tokens(500, 20)

    # Nothing appended since the observation → exactly the observed total.
    assert await agent._measure_context_tokens() == 520

    trailing = _assistant("x" * 400)
    agent.state.context.append(trailing)
    measured = await agent._measure_context_tokens()
    assert measured > 520
    assert measured == 520 + S.estimate_history_tokens([msg_to_history_dict(trailing)])


@pytest.mark.asyncio
async def test_measure_falls_back_to_framework_estimate_without_usage():
    """No provider usage yet (first step of a turn) → AgentScope's count_tokens."""
    agent = _make_agent([_user("a")])

    async def _prepare():
        return {"messages": [], "tools": []}

    async def _count(**_kwargs):
        return 4242

    agent._prepare_model_input = _prepare
    agent.model.count_tokens = _count

    assert await agent._measure_context_tokens() == 4242


# ── Trigger + replacement ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_below_threshold_does_not_call_engine(monkeypatch):
    agent = _make_agent([_user("hello"), _assistant("hi")], window=1000)
    agent.observe_context_tokens(10, 1)

    called = []

    async def _engine(chat_id, history):
        called.append(chat_id)
        return [{"role": "user", "content": "nope"}]

    monkeypatch.setattr(S, "run_mid_turn_compaction", _engine)
    await agent.compress_context()

    assert called == []
    assert len(agent.state.context) == 2


@pytest.mark.asyncio
async def test_over_threshold_installs_summary_then_recent_steps(monkeypatch):
    """The applied shape is Pi's: the summary first, then the recent region verbatim."""
    agent = _make_agent(
        [_user("第一个问题"), _assistant("很长的回答"), _user("第二个问题")], window=1000
    )
    agent.observe_context_tokens(900, 10)

    summary_text = C.format_summary_text("已完成第一步")
    captured = {}

    async def _engine(chat_id, history):
        captured["chat_id"] = chat_id
        captured["history"] = history
        _older, recent = C.split_history_for_compaction(history, keep_recent_tokens=5)
        return C.build_compacted_history(summary_text, recent)

    monkeypatch.setattr(S, "run_mid_turn_compaction", _engine)
    await agent.compress_context()

    assert captured["chat_id"] == "chat-1"
    # The engine sees the live context, assistant turns included.
    assert [m["role"] for m in captured["history"]] == ["user", "assistant", "user"]

    texts = [m.get_text_content() for m in agent.state.context]
    assert texts[0] == summary_text
    assert C.is_summary_message(texts[0])
    assert texts[1:] == ["第二个问题"]
    # The summary lives in the context, not state.summary, so it sits exactly
    # where the summarized region used to be.
    assert agent.state.summary == ""


@pytest.mark.asyncio
async def test_engine_failure_falls_back_to_framework_compression(monkeypatch):
    agent = _make_agent([_user("q"), _assistant("a")], window=1000)
    agent.observe_context_tokens(900, 10)

    async def _engine(chat_id, history):
        return None

    fallback_calls = []

    async def _framework(self, context_config=None):
        fallback_calls.append(context_config)

    monkeypatch.setattr(S, "run_mid_turn_compaction", _engine)
    monkeypatch.setattr("agentscope.agent.Agent.compress_context", _framework)

    await agent.compress_context()

    assert len(fallback_calls) == 1
    # The observation is dropped either way, so the next step re-measures.
    assert agent._jx_observation is None


@pytest.mark.asyncio
async def test_disabled_flag_keeps_framework_overflow_protection(monkeypatch):
    """CHAT_COMPACT_ENABLED=false turns off checkpointing, not overflow protection."""
    import core.config.settings as settings_mod

    agent = _make_agent([_user("q"), _assistant("a")], window=1000)
    agent.observe_context_tokens(900, 10)

    monkeypatch.setattr(
        settings_mod, "settings", SimpleNamespace(compaction=SimpleNamespace(enabled=False))
    )

    engine_calls = []

    async def _engine(chat_id, history):
        engine_calls.append(1)
        return [{"role": "user", "content": "sum"}]

    fallback_calls = []

    async def _framework(self, context_config=None):
        fallback_calls.append(1)

    monkeypatch.setattr(S, "run_mid_turn_compaction", _engine)
    monkeypatch.setattr("agentscope.agent.Agent.compress_context", _framework)

    await agent.compress_context()

    assert engine_calls == []
    assert len(fallback_calls) == 1


@pytest.mark.asyncio
async def test_does_not_recompact_when_context_did_not_grow(monkeypatch):
    """Convergence guard: an unshrinkable context must not burn a summary per step."""
    agent = _make_agent([_user("x" * 50), _assistant("y" * 50)], window=1000)
    agent.observe_context_tokens(900, 10)

    calls = []

    async def _engine(chat_id, history):
        calls.append(1)
        return [{"role": "user", "content": "x" * 50}, {"role": "user", "content": "sum"}]

    monkeypatch.setattr(S, "run_mid_turn_compaction", _engine)

    await agent.compress_context()
    assert len(calls) == 1

    # Still over the limit, context unchanged since → skipped, not re-summarized.
    agent.observe_context_tokens(900, 10)
    await agent.compress_context()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_offloaded_path_is_appended_to_the_summary(monkeypatch):
    class _Offloader:
        def __init__(self):
            self.msgs = None

        async def offload_context(self, session_id, msgs):
            self.msgs = msgs
            return "/workspace/.offload/ctx-1.json"

    offloader = _Offloader()
    agent = _make_agent([_user("q"), _assistant("a")], window=1000, offloader=offloader)
    agent.observe_context_tokens(900, 10)

    async def _engine(chat_id, history):
        return [{"role": "user", "content": "SUMMARY"}, {"role": "user", "content": "q"}]

    monkeypatch.setattr(S, "run_mid_turn_compaction", _engine)
    await agent.compress_context()

    assert offloader.msgs is not None and len(offloader.msgs) == 2
    head = agent.state.context[0].get_text_content()
    assert "/workspace/.offload/ctx-1.json" in head
    assert head.startswith("SUMMARY")
    # The verbatim tail is not touched by the reminder.
    assert agent.state.context[1].get_text_content() == "q"


@pytest.mark.asyncio
async def test_no_chat_id_still_compacts_without_checkpoint(monkeypatch):
    """Sub-agents and plan mode have no persisted session; they still compact."""
    import dataclasses

    agent = _make_agent([_user("q"), _assistant("a")], window=1000, chat_id="")
    agent.observe_context_tokens(900, 10)
    real = S.settings
    monkeypatch.setattr(
        S,
        "settings",
        dataclasses.replace(
            real, compaction=dataclasses.replace(real.compaction, keep_recent_tokens=0)
        ),
    )

    seen = {}

    async def _summarize(history, *, timeout):
        seen["n"] = len(history)
        return "摘要正文"

    writes = []
    monkeypatch.setattr(S, "_summarize", _summarize)
    monkeypatch.setattr(
        S, "run_compaction", lambda *a, **k: writes.append(1)
    )  # must not be reached

    await agent.compress_context()

    assert writes == []
    assert seen["n"] == 2
    assert C.is_summary_message(agent.state.context[-1].get_text_content())


# ── Message conversion ───────────────────────────────────────────────────────


def test_msg_to_history_dict_preserves_tool_blocks():
    """Tool blocks stay blocks so the engine renders them its one way."""
    msg = Msg(
        name="agent",
        content=[
            TextBlock(type="text", text="正在检索"),
            ToolCallBlock(type="tool_call", id="t1", name="search", input='{"q": "北京"}'),
        ],
        role="assistant",
    )
    d = msg_to_history_dict(msg)
    assert d["role"] == "assistant"
    types = [b["type"] for b in d["content"]]
    assert types == ["text", "tool_call"]

    rendered = S._render_content_for_summary(d["content"])
    assert "正在检索" in rendered
    assert "[tool_call search]" in rendered

    result = Msg(
        name="agent",
        content=[ToolResultBlock(type="tool_result", id="t1", name="search", output="命中 3 条")],
        role="assistant",
    )
    rendered_result = S._render_content_for_summary(msg_to_history_dict(result)["content"])
    assert "[tool_result search]" in rendered_result
    assert "命中 3 条" in rendered_result


# ── One trigger ratio ────────────────────────────────────────────────────────


def test_trigger_ratio_console_value_wins(monkeypatch):
    monkeypatch.setattr(S, "_RATIO_MEMO", None)
    monkeypatch.setattr(
        S, "settings", SimpleNamespace(compaction=SimpleNamespace(trigger_ratio=0.8))
    )

    class _Svc:
        @staticmethod
        def get_instance():
            return _Svc()

        def get(self, key):
            assert key == "chat.compress_in_turn_ratio"
            return "0.7"

    monkeypatch.setitem(
        __import__("sys").modules,
        "core.services.system_config",
        SimpleNamespace(SystemConfigService=_Svc),
    )
    assert S.resolve_trigger_ratio() == 0.7


def test_trigger_ratio_out_of_range_ignored(monkeypatch):
    monkeypatch.setattr(S, "_RATIO_MEMO", None)
    monkeypatch.setattr(
        S, "settings", SimpleNamespace(compaction=SimpleNamespace(trigger_ratio=0.8))
    )

    class _Svc:
        @staticmethod
        def get_instance():
            return _Svc()

        def get(self, key):
            return "0.99"

    monkeypatch.setitem(
        __import__("sys").modules,
        "core.services.system_config",
        SimpleNamespace(SystemConfigService=_Svc),
    )
    assert S.resolve_trigger_ratio() == 0.8


def test_token_limit_is_pure_and_takes_the_ratio_from_its_caller(monkeypatch):
    """No DB read here: this runs at every step boundary and on the pre-turn fast path."""
    monkeypatch.setattr(
        S, "settings", SimpleNamespace(compaction=SimpleNamespace(token_limit=0, trigger_ratio=0.8))
    )

    def _explode():
        raise AssertionError("resolve_token_limit must not resolve the console ratio itself")

    monkeypatch.setattr(S, "resolve_trigger_ratio", _explode)

    assert S.resolve_token_limit(200_000, ratio=0.75) == 150_000
    # No ratio passed → the env default, never a console read.
    assert S.resolve_token_limit(200_000) == 160_000
    assert S.resolve_token_limit(None) is None


@pytest.mark.asyncio
async def test_measure_counts_tool_results_appended_inside_existing_assistant():
    agent = _make_agent([_user("q"), _assistant("answer")])
    agent.observe_context_tokens(500, 20)
    agent.state.context[-1].content.append(
        ToolResultBlock(id="t1", name="search", output="R" * 50_000)
    )

    measured = await agent._measure_context_tokens()
    assert measured > 12_500, "the 50k result must count even without a new message"
    assert await agent._measure_context_tokens() == measured, "do not accumulate estimates twice"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["append_block", "edit_block", "replace_message", "summary"])
async def test_changed_content_can_compact_again_without_more_messages(monkeypatch, change):
    agent = _make_agent([_user("q"), _assistant("answer")])
    calls = []

    async def engine(chat_id, history):
        calls.append(history)
        return [
            {"role": "user", "content": "summary"},
            {"role": "assistant", "content": [{"type": "text", "text": "recent"}]},
        ]

    monkeypatch.setattr(S, "run_mid_turn_compaction", engine)
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
async def test_rewritten_history_invalidates_the_usage_baseline(monkeypatch, change):
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

    async def prepare():
        return {}

    async def count(**kwargs):
        return 4242

    monkeypatch.setattr(agent, "_prepare_model_input", prepare)
    agent.model.count_tokens = count
    assert await agent._measure_context_tokens() == 4242
    assert agent._jx_observation is None


@pytest.mark.asyncio
async def test_actual_sdk_event_order_does_not_double_count_response(monkeypatch):
    from agentscope.event import ModelCallEndEvent
    from agentscope.model import ChatResponse
    from orchestration.streaming import StreamingAgent

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
