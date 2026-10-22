"""Canonical model history remains independent of personal-agent display splitting."""

import pytest
from core.db.models import ChatMessage
from core.llm.runtime.model_steps import record_assistant_step, record_tool_result_step
from core.chat.compaction.history import normalize_rows
from tests.test_personal_agent_monitor import sessions as sessions
from tests.test_personal_task_transcript_order import prepared as prepared
from tests.test_personal_task_transcript_order import running as running
from tests.test_personal_task_transcript_order import update


def assistant(*blocks):
    return record_assistant_step(blocks, provider="p", model="m", protocol="openai_chat")


def model_history(sessions, log):
    with sessions() as db:
        rows = (
            db.query(ChatMessage)
            .filter_by(chat_id=log.chat_id)
            .order_by(ChatMessage.chat_seq)
            .all()
        )
        return normalize_rows(rows)


@pytest.mark.parametrize("split_display", [False, True])
def test_completed_response_stays_before_mid_response_user_input(running, sessions, split_display):
    ident, log = running
    log.record("text_delta", "FIRST")
    update(sessions, ident, "NEW-USER-INPUT", "steer")
    if split_display:
        log.record("text_delta", "-SECOND")
    text = "FIRST-SECOND" if split_display else "FIRST"
    log.record("model_step", assistant({"type": "text", "text": text}))
    log.flush()
    history = model_history(sessions, log)
    assert [row["role"] for row in history] == ["user", "assistant", "user"]
    assert history[1]["content"] == [{"type": "text", "text": text}]
    assert history[2]["content"] == "NEW-USER-INPUT"


def test_tool_result_stays_with_original_call_after_input_split(running, sessions):
    ident, log = running
    call = {"type": "tool_call", "id": "first-call", "name": "pa_bash", "input": "{}"}
    log.record("model_step", assistant(call))
    log.record("tool_call", {"id": "first-call", "name": "pa_bash", "args": {}})
    update(sessions, ident, "NEW-USER-INPUT", "steer")
    log.record("thinking_delta", "new display")
    result = record_tool_result_step(
        {
            "type": "tool_result",
            "id": "first-call",
            "name": "pa_bash",
            "output": "REAL-RESULT",
            "state": "success",
        },
    )
    log.record("model_step", result)
    log.record(
        "tool_result",
        {"id": "first-call", "content": "REAL-RESULT", "status": "success"},
    )
    log.flush()
    history = model_history(sessions, log)
    assert [row["role"] for row in history] == ["user", "assistant", "tool", "user"]
    assert history[2]["content"][0]["output"] == "REAL-RESULT"


def test_tool_result_survives_later_flush_on_same_row(running, sessions):
    _, log = running
    log.record(
        "model_step",
        assistant({"type": "tool_call", "id": "call", "name": "pa_bash", "input": "{}"}),
    )
    log.record("tool_call", {"id": "call", "name": "pa_bash", "args": {}})
    log.record(
        "model_step",
        record_tool_result_step(
            {
                "type": "tool_result",
                "id": "call",
                "name": "pa_bash",
                "output": "REAL-RESULT",
                "state": "success",
            },
        ),
    )
    log.record("tool_result", {"id": "call", "content": "REAL-RESULT", "status": "success"})
    log.record("text_delta", "done")
    log.record("model_step", assistant({"type": "text", "text": "done"}))
    log.flush()
    history = model_history(sessions, log)
    assert history[2]["content"][0]["output"] == "REAL-RESULT"
    with sessions() as db:
        assert db.get(ChatMessage, log.message_id).tool_calls[0]["result"] == "REAL-RESULT"
