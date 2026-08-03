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
