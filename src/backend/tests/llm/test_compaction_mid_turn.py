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
    agent._jx_compacted_at_len = None
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
async def test_over_threshold_installs_replacement_with_summary_last(monkeypatch):
    """The applied shape is Codex's: recent user messages, summary trailing."""
    agent = _make_agent(
        [_user("第一个问题"), _assistant("很长的回答"), _user("第二个问题")], window=1000
    )
    agent.observe_context_tokens(900, 10)

    summary_text = C.format_summary_text("已完成第一步")
    captured = {}

    async def _engine(chat_id, history):
        captured["chat_id"] = chat_id
        captured["history"] = history
        return C.build_compacted_history(C.collect_user_messages(history), summary_text)

    monkeypatch.setattr(S, "run_mid_turn_compaction", _engine)
    await agent.compress_context()

    assert captured["chat_id"] == "chat-1"
    # The engine sees the live context, assistant turns included.
    assert [m["role"] for m in captured["history"]] == ["user", "assistant", "user"]

    texts = [m.get_text_content() for m in agent.state.context]
    assert texts[-1] == summary_text
    assert C.is_summary_message(texts[-1])
    assert texts[:-1] == ["第一个问题", "第二个问题"]
    # The summary lives in the context, not state.summary: AgentScope renders
    # state.summary right after the system prompt, Codex puts it last.
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
        return [{"role": "user", "content": "q"}, {"role": "user", "content": "SUMMARY"}]

    monkeypatch.setattr(S, "run_mid_turn_compaction", _engine)
    await agent.compress_context()

    assert offloader.msgs is not None and len(offloader.msgs) == 2
    tail = agent.state.context[-1].get_text_content()
    assert "/workspace/.offload/ctx-1.json" in tail
    assert tail.startswith("SUMMARY")


@pytest.mark.asyncio
async def test_no_chat_id_still_compacts_without_checkpoint(monkeypatch):
    """Sub-agents and plan mode have no persisted session; they still compact."""
    agent = _make_agent([_user("q"), _assistant("a")], window=1000, chat_id="")
    agent.observe_context_tokens(900, 10)

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
