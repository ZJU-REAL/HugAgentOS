"""model_progress liveness signal tests.

During long tool-call-argument generation the model streams ToolCallDeltaEvent
for minutes while StreamingAgent._map_event yields nothing downstream; the run
inactivity watchdog then saw pure silence and killed a healthy run. stream()
now emits a throttled ("model_progress", None) when upstream events keep
arriving but none maps to an SSE event.
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
async def test_swallowed_deltas_emit_model_progress(monkeypatch):
    monkeypatch.setattr(streaming_mod, "_MODEL_PROGRESS_MIN_INTERVAL_S", 0.0)
    events = [ToolCallStartEvent()] + [ToolCallDeltaEvent(delta="chunk") for _ in range(5)]
    out = await _collect(_fake_agent(events))
    types = [t for t, _ in out]
    assert "tool_pending" in types
    assert types.count("model_progress") == 5


@pytest.mark.asyncio
async def test_throttle_suppresses_model_progress():
    # Default 5s throttle: a fast burst of swallowed deltas must not spam signals.
    events = [ToolCallStartEvent()] + [ToolCallDeltaEvent(delta="chunk") for _ in range(5)]
    out = await _collect(_fake_agent(events))
    types = [t for t, _ in out]
    assert types.count("model_progress") == 0


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
async def test_silence_without_inflight_model_call_stays_heartbeat(monkeypatch):
    # Same silence but no model call in flight (e.g. a tool executing) keeps the
    # historical heartbeat semantics — excluded from watchdog activity.
    monkeypatch.setattr(streaming_mod, "_MODEL_PROGRESS_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(streaming_mod, "_QUEUE_POLL_INTERVAL_S", 0.01)
    out = await _collect(_slow_model_agent([], silence_s=0.1))
    types = [t for t, _ in out]
    assert types.count("heartbeat") >= 2
    assert types.count("model_progress") == 0
