"""Streaming deltas are merged before they become durable run events."""
import asyncio
from types import SimpleNamespace

import pytest

from core.llm.runtime import streaming_constants
from core.llm.runtime.delta_coalescer import DeltaCoalescer
from core.llm.runtime.streaming import StreamingAgent


def tool_delta(text, tid="t1"):
    return ("tool_call_delta", {"name": "bash", "id": tid, "delta": text})


def test_first_delta_is_immediate_and_followers_merge_until_due():
    deltas = DeltaCoalescer()
    assert deltas.feed(("text_delta", "a"), now=10.0) == [("text_delta", "a")]
    assert deltas.feed(("text_delta", "b"), now=10.01) == []
    assert deltas.feed(("text_delta", "c"), now=10.02) == []
    assert deltas.deadline() == pytest.approx(10.0 + streaming_constants.DELTA_FLUSH_INTERVAL_S)
    assert deltas.flush_due(now=10.03) == []
    assert deltas.flush_due(now=10.06) == [("text_delta", "bc")]
    assert deltas.deadline() is None


def test_character_threshold_flushes_without_waiting(monkeypatch):
    monkeypatch.setattr(streaming_constants, "DELTA_FLUSH_CHARS", 4)
    deltas = DeltaCoalescer()
    deltas.feed(("thinking_delta", "x"), now=0.0)
    assert deltas.feed(("thinking_delta", "ab"), now=0.01) == []
    assert deltas.feed(("thinking_delta", "cd"), now=0.02) == [("thinking_delta", "abcd")]


def test_switching_stream_or_any_other_event_flushes_first_in_order():
    deltas = DeltaCoalescer()
    deltas.feed(("thinking_delta", "plan"), now=0.0)
    assert deltas.feed(("thinking_delta", " more"), now=0.01) == []
    # A different stream flushes the buffered one before itself is buffered.
    assert deltas.feed(("text_delta", "answer"), now=0.02) == [("thinking_delta", " more")]
    assert deltas.feed(("tool_call_start", {"id": "t1"}), now=0.03) == [
        ("text_delta", "answer"),
        ("tool_call_start", {"id": "t1"}),
    ]
    deltas.feed(tool_delta('{"a"', "t1"), now=1.0)
    deltas.feed(tool_delta(":1}", "t1"), now=1.01)
    # Parallel tool calls are separate streams.
    assert deltas.feed(tool_delta("{", "t2"), now=1.02) == [tool_delta(":1}", "t1")]
    assert deltas.drain(now=1.03) == [tool_delta("{", "t2")]


def test_empty_deltas_are_dropped_and_reset_discards():
    deltas = DeltaCoalescer()
    assert deltas.feed(("text_delta", ""), now=0.0) == []
    deltas.feed(("text_delta", "a"), now=0.0)
    deltas.feed(("text_delta", "b"), now=0.01)
    deltas.reset()
    assert deltas.drain() == []
    # After a reset the clock is forgotten: the next delta goes out at once.
    assert deltas.feed(("text_delta", "c"), now=0.02) == [("text_delta", "c")]


class TextBlockDeltaEvent:
    def __init__(self, delta):
        self.delta = delta


class ToolCallStartEvent:
    tool_call_id = "t1"
    tool_call_name = "bash"


def _agent(script):
    async def reply_stream(inputs=None):
        async for event in script():
            yield event

    state = SimpleNamespace(
        user_id="u1",
        chat_id="c1",
        apply_request_context=lambda ctx, text: None,
        context=SimpleNamespace(extend=lambda msgs: None),
        pending_mcp_progress=[],
    )
    return SimpleNamespace(state=state, model=None, reply_stream=reply_stream)


async def _collect(agent):
    stream = StreamingAgent(agent, mcp_clients=[])
    return [item async for item in stream.stream(session_messages=[], context={"enable_thinking": True})]


@pytest.mark.asyncio
async def test_token_stream_becomes_few_events_with_identical_text():
    tokens = [f"t{i} " for i in range(200)]

    async def script():
        for token in tokens:
            yield TextBlockDeltaEvent(token)

    out = await _collect(_agent(script))
    texts = [payload for kind, payload in out if kind == "text_delta"]
    assert "".join(texts) == "".join(tokens)
    assert len(texts) < len(tokens) / 10


@pytest.mark.asyncio
async def test_buffered_tail_is_flushed_on_deadline_while_the_model_is_silent(monkeypatch):
    monkeypatch.setattr(streaming_constants, "QUEUE_POLL_INTERVAL_S", 5.0)
    seen = []
    released = asyncio.Event()

    async def script():
        yield TextBlockDeltaEvent("Hel")
        yield TextBlockDeltaEvent("lo")
        await released.wait()
        yield ToolCallStartEvent()

    stream = StreamingAgent(_agent(script), mcp_clients=[])
    events = stream.stream(session_messages=[], context={"enable_thinking": True})
    seen.append(await anext(events))
    # "lo" is buffered; it must arrive on its deadline, not after the 5 s poll.
    seen.append(await asyncio.wait_for(anext(events), timeout=1.0))
    released.set()
    seen.extend([item async for item in events])
    assert seen[:2] == [("text_delta", "Hel"), ("text_delta", "lo")]
    assert seen[2] == ("tool_call_start", {"name": "bash", "id": "t1"})
    assert ("heartbeat", None) not in seen


@pytest.mark.asyncio
async def test_tail_is_flushed_before_tool_start_and_at_the_end():
    async def script():
        yield TextBlockDeltaEvent("a")
        yield TextBlockDeltaEvent("b")
        yield ToolCallStartEvent()
        yield TextBlockDeltaEvent("c")
        yield TextBlockDeltaEvent("d")

    out = await _collect(_agent(script))
    kinds = [kind for kind, _ in out]
    assert kinds.index("tool_call_start") > 0
    before = "".join(p for k, p in out[: kinds.index("tool_call_start")] if k == "text_delta")
    after = "".join(p for k, p in out[kinds.index("tool_call_start") :] if k == "text_delta")
    assert (before, after) == ("ab", "cd")
