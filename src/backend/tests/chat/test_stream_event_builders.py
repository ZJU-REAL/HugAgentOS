"""Lock the SSE event-builder output shared by the chat route and the
background run executor (core.chat.tool_log).

These three builders were extracted from the two near-identical SSE loops in
``api/routes/v1/chats.py`` and ``orchestration/chat_run_executor.py``. The
tests pin the exact event dicts + log side-effects so the two call sites stay
byte-identical and a future change can't silently diverge them.
"""

from core.chat.tool_log import (
    build_thinking_event,
    build_tool_call_event,
    build_tool_result_event,
)


def test_thinking_event_delta():
    assert build_thinking_event({"type": "thinking", "delta": "想"}, "c1") == {
        "type": "thinking",
        "chat_id": "c1",
        "delta": "想",
    }


def test_thinking_event_message_fallback():
    # No "delta" key → falls back to "message" with the default hint.
    assert build_thinking_event({"type": "thinking"}, "c1") == {
        "type": "thinking",
        "chat_id": "c1",
        "message": "正在思考...",
    }


def test_tool_call_event_and_log_upsert():
    log: list = []
    chunk = {
        "type": "tool_call",
        "tool_name": "bash",
        "tool_display_name": "Bash",
        "tool_args": {"command": "ls"},
        "tool_id": "t1",
    }
    evt = build_tool_call_event(chunk, "c1", log)
    assert evt == {
        "type": "tool_call",
        "tool_name": "bash",
        "tool_display_name": "Bash",
        "tool_args": {"command": "ls"},
        "tool_id": "t1",
        "chat_id": "c1",
    }
    # The builder upserts the tool_call into the log (without chat_id/type).
    assert log == [{
        "tool_name": "bash",
        "tool_display_name": "Bash",
        "tool_args": {"command": "ls"},
        "tool_id": "t1",
    }]


def test_tool_call_event_subagent_passthrough():
    log: list = []
    evt = build_tool_call_event(
        {"tool_name": "x", "tool_id": "t", "subagent_name": "planner"}, "c1", log
    )
    assert evt["subagent_name"] == "planner"
    assert log[0]["subagent_name"] == "planner"


def test_tool_result_event_and_log_attach():
    log = [{"tool_name": "bash", "tool_id": "t1"}]
    chunk = {
        "type": "tool_result",
        "tool_id": "t1",
        "tool_name": "bash",
        "result": {"stdout": "ok"},
        "citations": [{"n": 1}],
    }
    evt = build_tool_result_event(chunk, "c1", log)
    assert evt == {
        "type": "tool_result",
        "tool_name": "bash",
        "result": {"stdout": "ok"},
        "tool_id": "t1",
        "chat_id": "c1",
        "citations": [{"n": 1}],
    }
    # The result is attached onto the matching log entry.
    assert log[0]["result"] == {"stdout": "ok"}
    assert log[0]["status"] == "success"


def test_tool_result_event_defaults_when_missing():
    evt = build_tool_result_event({"tool_id": "t9"}, "c1", [])
    assert evt["result"] == {}
    assert evt["citations"] == []
    assert "subagent_name" not in evt
