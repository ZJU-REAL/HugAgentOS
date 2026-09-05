"""Tool-call delta batching and model_progress liveness tests.

Small argument fragments are buffered briefly to avoid an SSE storm. While a
fragment has not crossed either flush threshold, ``model_progress`` still keeps
the run watchdog alive; flushed fragments become visible ``tool_call_delta``
events and the completed JSON is emitted exactly once as ``tool_call``.
"""

import asyncio
from types import SimpleNamespace

import pytest

import orchestration.streaming as streaming_mod
from orchestration.streaming import StreamingAgent


class ToolCallStartEvent:  # noqa: D101 - name-dispatched by _map_event
    def __init__(self, tid="t1", name="bash"):
        self.tool_call_id = tid
        self.tool_call_name = name


class ModelCallStartEvent:  # noqa: D101 - name-dispatched by _map_event
    pass


class ToolCallDeltaEvent:  # noqa: D101
    def __init__(self, tid="t1", delta="x"):
        self.tool_call_id = tid
        self.delta = delta


class ToolCallEndEvent:  # noqa: D101
    def __init__(self, tid="t1"):
        self.tool_call_id = tid


def _fake_agent(events):
    async def reply_stream(inputs=None):
        for ev in events:
            yield ev

    state = SimpleNamespace(
        user_id="u1",
        chat_id="c1",
        apply_request_context=lambda ctx, text: None,
        context=SimpleNamespace(extend=lambda msgs: None),
    )
    return SimpleNamespace(state=state, model=None, reply_stream=reply_stream)


async def _collect(agent):
    sa = StreamingAgent(agent, mcp_clients=[])
    out = []
    async for item in sa.stream(session_messages=[], context={"enable_thinking": True}):
        out.append(item)
    return out


@pytest.mark.asyncio
async def test_small_buffered_deltas_emit_model_progress_until_flushed(monkeypatch):
    monkeypatch.setattr(streaming_mod, "_MODEL_PROGRESS_MIN_INTERVAL_S", 0.0)
    events = [ToolCallStartEvent()] + [ToolCallDeltaEvent(delta="chunk") for _ in range(5)]
    out = await _collect(_fake_agent(events))
    types = [t for t, _ in out]
    assert "tool_call_start" in types
    assert types.count("model_progress") == 5


@pytest.mark.asyncio
async def test_throttle_suppresses_model_progress():
    # Default 5s throttle: a fast burst of swallowed deltas must not spam signals.
    events = [ToolCallStartEvent()] + [ToolCallDeltaEvent(delta="chunk") for _ in range(5)]
    out = await _collect(_fake_agent(events))
    types = [t for t, _ in out]
    assert types.count("model_progress") == 0


@pytest.mark.asyncio
async def test_tool_arguments_stream_as_bounded_deltas_then_one_final_call(monkeypatch):
    monkeypatch.setattr(streaming_mod, "_TOOL_CALL_DELTA_FLUSH_CHARS", 8)
    monkeypatch.setattr(streaming_mod, "_TOOL_CALL_DELTA_FLUSH_INTERVAL_S", 999.0)
    events = [
        ToolCallStartEvent(),
        ToolCallDeltaEvent(delta='{"command"'),
        ToolCallDeltaEvent(delta=':"echo ok"}'),
        ToolCallEndEvent(),
    ]
    out = await _collect(_fake_agent(events))

    assert out[0] == ("tool_call_start", {"name": "bash", "id": "t1"})
    deltas = [payload["delta"] for kind, payload in out if kind == "tool_call_delta"]
    assert "".join(deltas) == '{"command":"echo ok"}'
    calls = [payload for kind, payload in out if kind == "tool_call"]
    assert calls == [{"name": "bash", "args": {"command": "echo ok"}, "id": "t1"}]


def _slow_model_agent(pre_events, silence_s):
    """Agent that emits ``pre_events`` then goes silent (model call hanging)."""

    async def reply_stream(inputs=None):
        for ev in pre_events:
            yield ev
        await asyncio.sleep(silence_s)

    state = SimpleNamespace(
        user_id="u1",
        chat_id="c1",
        apply_request_context=lambda ctx, text: None,
        context=SimpleNamespace(extend=lambda msgs: None),
    )
    return SimpleNamespace(state=state, model=None, reply_stream=reply_stream)


@pytest.mark.asyncio
async def test_inflight_model_call_silence_emits_model_progress(monkeypatch):
    # A model call in flight (ModelCallStart seen, nothing since — hung/slow LLM
    # endpoint) must surface as model_progress, not heartbeat, so the run
    # inactivity watchdog does not kill a run that is merely waiting on the model.
    monkeypatch.setattr(streaming_mod, "_MODEL_PROGRESS_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(streaming_mod, "_QUEUE_POLL_INTERVAL_S", 0.01)
    out = await _collect(_slow_model_agent([ModelCallStartEvent()], silence_s=0.1))
    types = [t for t, _ in out]
    assert types.count("model_progress") >= 2
    assert types.count("heartbeat") == 0


@pytest.mark.asyncio
async def test_model_call_start_surfaces_the_real_dispatch_boundary(monkeypatch):
    monkeypatch.setattr(streaming_mod, "_QUEUE_POLL_INTERVAL_S", 0.01)
    out = await _collect(_slow_model_agent([ModelCallStartEvent()], silence_s=0.01))

    assert ("model_call_start", None) in out


@pytest.mark.asyncio
async def test_silence_without_inflight_model_call_stays_heartbeat(monkeypatch):
    # Same silence but no model call in flight (e.g. a tool executing) keeps the
    # historical heartbeat semantics — excluded from watchdog activity.
    monkeypatch.setattr(streaming_mod, "_MODEL_PROGRESS_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(streaming_mod, "_QUEUE_POLL_INTERVAL_S", 0.01)
    out = await _collect(_slow_model_agent([], silence_s=0.1))
    types = [t for t, _ in out]
    assert types.count("heartbeat") >= 2
    assert types.count("model_progress") == 0


@pytest.mark.asyncio
async def test_user_question_resolved_is_drained_before_immediate_stream_done(monkeypatch):
    """A fast terminal event must not overtake the server-owned resolution."""

    from core.llm.tools import user_questions

    signal_q = asyncio.Queue()

    async def reply_stream(inputs=None):
        del inputs
        signal_q.put_nowait(
            {"event": "resolved", "request_id": "req-fast", "outcome": "answered"},
        )
        if False:
            yield None

    agent = _fake_agent([])
    agent.reply_stream = reply_stream
    monkeypatch.setattr(user_questions, "get_ui_queue", lambda _chat_id: signal_q)

    out = await _collect(agent)
    assert (
        "user_question_resolved",
        {"event": "resolved", "request_id": "req-fast", "outcome": "answered"},
    ) in out
