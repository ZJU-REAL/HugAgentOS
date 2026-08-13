"""Streaming argument coverage for nested sub-agent tool calls."""

import core.llm.subagent_tool as subagent_mod
from core.llm.subagent_tool import _SubMapper


class ToolCallStartEvent:  # noqa: D101 - name-dispatched by _SubMapper
    def __init__(self, tid="nested-1", name="bash"):
        self.tool_call_id = tid
        self.tool_call_name = name


class ToolCallDeltaEvent:  # noqa: D101
    def __init__(self, delta, tid="nested-1"):
        self.tool_call_id = tid
        self.delta = delta


class ToolCallEndEvent:  # noqa: D101
    def __init__(self, tid="nested-1"):
        self.tool_call_id = tid


def test_subagent_tool_arguments_stream_then_finish_once(monkeypatch):
    monkeypatch.setattr(subagent_mod, "_TOOL_CALL_DELTA_FLUSH_CHARS", 8)
    monkeypatch.setattr(subagent_mod, "_TOOL_CALL_DELTA_FLUSH_INTERVAL_S", 999.0)
    mapper = _SubMapper()

    output = mapper.feed(ToolCallStartEvent())
    output += mapper.feed(ToolCallDeltaEvent('{"command"'))
    output += mapper.feed(ToolCallDeltaEvent(':"echo ok"}'))
    output += mapper.feed(ToolCallEndEvent())

    starts_and_final = [item for item in output if item["sub_type"] == "tool_call"]
    assert starts_and_final == [
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
