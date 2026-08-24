"""Lock the SSE event-builder output shared by the chat route and the
background run executor (core.chat.tool_log).

These builders are shared by the two near-identical SSE loops in
``api/routes/v1/chats.py`` and ``orchestration/chat_run_executor.py``. The
tests pin the exact event dicts + log side-effects so the two call sites stay
byte-identical and a future change can't silently diverge them.
"""

from core.chat.tool_log import (
    build_thinking_event,
    build_tool_call_delta_event,
    build_tool_call_event,
    build_tool_call_start_event,
    build_tool_result_event,
    build_user_question_event,
    build_user_question_resolved_event,
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


def test_tool_call_start_and_delta_are_transient_wire_events():
    start = build_tool_call_start_event(
        {
            "tool_name": "bash",
            "tool_display_name": "Bash",
            "tool_id": "t1",
        },
        "c1",
    )
    assert start == {
        "type": "tool_call_start",
        "tool_name": "bash",
        "tool_display_name": "Bash",
        "tool_id": "t1",
        "chat_id": "c1",
    }

    delta = build_tool_call_delta_event(
        {
            "tool_name": "bash",
            "tool_id": "t1",
            "arguments_delta": '{"command":"ec',
        },
        "c1",
    )
    assert delta == {
        "type": "tool_call_delta",
        "tool_name": "bash",
        "tool_id": "t1",
        "arguments_delta": '{"command":"ec',
        "chat_id": "c1",
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
    assert log == [
        {
            "tool_name": "bash",
            "tool_display_name": "Bash",
            "tool_args": {"command": "ls"},
            "tool_id": "t1",
        }
    ]


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
        "status": "success",
    }
    # The result is attached onto the matching log entry.
    assert log[0]["result"] == {"stdout": "ok"}
    assert log[0]["status"] == "success"


def test_tool_result_event_defaults_when_missing():
    evt = build_tool_result_event({"tool_id": "t9"}, "c1", [])
    assert evt["result"] == {}
    assert evt["citations"] == []
    assert "subagent_name" not in evt


def test_interrupted_tool_result_keeps_neutral_status():
    log = [{"tool_name": "bash", "tool_id": "t1"}]
    evt = build_tool_result_event(
        {
            "tool_id": "t1",
            "tool_name": "bash",
            "result": {"message": "interrupted by steer"},
            "status": "interrupted",
        },
        "c1",
        log,
    )
    assert evt["status"] == "interrupted"
    assert log[0]["status"] == "interrupted"


def test_user_question_requested_and_resolved_wire_events():
    requested = build_user_question_event(
        {
            "request_id": "req-1",
            "questions": [{"id": "scope", "question": "范围？"}],
            "created_at": 10.0,
            "expires_at": 20.0,
        },
        "chat-1",
    )
    assert requested == {
        "type": "user_question",
        "chat_id": "chat-1",
        "request_id": "req-1",
        "questions": [{"id": "scope", "question": "范围？"}],
        "created_at": 10.0,
        "expires_at": 20.0,
    }

    resolved = build_user_question_resolved_event(
        {"request_id": "req-1", "outcome": "answered"},
        "chat-1",
    )
    assert resolved == {
        "type": "user_question_resolved",
        "chat_id": "chat-1",
        "request_id": "req-1",
        "outcome": "answered",
    }
