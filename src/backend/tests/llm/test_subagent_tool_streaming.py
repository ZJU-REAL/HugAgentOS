"""Sub-agent sidebar events reuse the main streaming event mapper."""

from types import SimpleNamespace

import pytest

from core.llm.subagent_tool import _SubMapper, _map_subagent_event
from orchestration.streaming import StreamingAgent
import orchestration.streaming as streaming_mod


class TextBlockDeltaEvent:
    def __init__(self, delta):
        self.delta = delta


class ToolCallStartEvent:
    def __init__(self, tid="nested-1", name="bash"):
        self.tool_call_id = tid
        self.tool_call_name = name


class ToolCallDeltaEvent:
    def __init__(self, delta, tid="nested-1"):
        self.tool_call_id = tid
        self.delta = delta


class ToolCallEndEvent:
    def __init__(self, tid="nested-1"):
        self.tool_call_id = tid


async def _map(stream, mapper, event):
    return await _map_subagent_event(stream, mapper, event)


def _stream(structured=False):
    agent = SimpleNamespace(model=SimpleNamespace(structured_reasoning=structured))
    stream = StreamingAgent(agent, mcp_clients=[])
    stream._enable_thinking = True
    return stream


@pytest.mark.asyncio
async def test_structured_subagent_content_streams_before_model_call_end():
    stream = _stream(structured=True)
    mapper = _SubMapper()

    first = await _map(stream, mapper, TextBlockDeltaEvent("hello"))
    second = await _map(stream, mapper, TextBlockDeltaEvent(" world"))

    assert first == [{"sub_type": "content", "delta": "hello"}]
    assert second == [{"sub_type": "content", "delta": " world"}]
    assert mapper.finish_turn() == []


@pytest.mark.asyncio
async def test_subagent_tool_arguments_use_main_mapper(monkeypatch):
    monkeypatch.setattr(streaming_mod, "_TOOL_CALL_DELTA_FLUSH_CHARS", 8)
    monkeypatch.setattr(streaming_mod, "_TOOL_CALL_DELTA_FLUSH_INTERVAL_S", 999.0)
    stream = _stream(structured=True)
    mapper = _SubMapper()

    output = await _map(stream, mapper, ToolCallStartEvent())
    output += await _map(stream, mapper, ToolCallDeltaEvent('{"command"'))
    output += await _map(stream, mapper, ToolCallDeltaEvent(':"echo ok"}'))
    output += await _map(stream, mapper, ToolCallEndEvent())

    assert [item for item in output if item["sub_type"] == "tool_call"] == [
        {
            "sub_type": "tool_call",
            "tool_id": "nested-1",
            "tool_name": "bash",
            "input": None,
            "status": "running",
        },
        {
            "sub_type": "tool_call",
            "tool_id": "nested-1",
            "tool_name": "bash",
            "input": {"command": "echo ok"},
            "status": "running",
        },
    ]
    deltas = [item["arguments_delta"] for item in output if item["sub_type"] == "tool_call_delta"]
    assert "".join(deltas) == '{"command":"echo ok"}'


def test_legacy_inline_thinking_still_splits():
    mapper = _SubMapper()
    assert mapper.feed("text_delta", "<think>reason") == []
    assert mapper.feed("text_delta", "</think>answer") == [
        {"sub_type": "thinking", "delta": "reason"},
        {"sub_type": "content", "delta": "answer"},
    ]
    assert mapper.feed("text_delta", " continues") == [
        {"sub_type": "content", "delta": " continues"},
    ]
    assert mapper.finish_turn() == []


def test_structured_thinking_remains_separate():
    mapper = _SubMapper()
    mapper.feed("reasoning_protocol", {"structured_reasoning": True})
    assert mapper.feed("thinking_delta", "reason") == [
        {"sub_type": "thinking", "delta": "reason"},
    ]
    assert mapper.feed("text_delta", "answer") == [
        {"sub_type": "content", "delta": "answer"},
    ]
