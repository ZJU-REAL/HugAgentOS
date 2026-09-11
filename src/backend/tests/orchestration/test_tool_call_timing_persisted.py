"""A tool card's clock must come from the server, not from the browser.

The log a message stores is what a reload — or the same conversation opened on
a second device — reads back. It used to carry the call's name, arguments and
result but no timing at all, so a replayed card had nothing to count from and
started its stopwatch at whatever moment it happened to be rendered. Every
switch between sessions restarted the numbers.

Both ends of the span are now recorded in the log: when the call opened, and
how long it took.
"""

from core.chat.tool_log import (
    attach_tool_result,
    build_tool_call_event,
    build_tool_result_event,
    upsert_tool_call,
)


def test_opening_a_call_records_when_it_started():
    log: list = []
    evt = build_tool_call_event(
        {"tool_name": "read_file", "tool_id": "call_1", "tool_args": {"path": "a.txt"}},
        "chat_1",
        log,
    )
    assert isinstance(log[0]["started_at"], int)
    # The same stamp goes out on the wire, so a live card and a replayed one
    # count from the same instant.
    assert evt["started_at"] == log[0]["started_at"]


def test_a_repeat_event_keeps_the_original_start():
    """Streamed arguments arrive after the card opens; that backfill must not
    reset the clock to the later moment."""
    log: list = []
    upsert_tool_call(log, {"tool_name": "read_file", "tool_id": "call_1", "tool_args": {}})
    opened_at = log[0]["started_at"]
    upsert_tool_call(
        log, {"tool_name": "read_file", "tool_id": "call_1", "tool_args": {"path": "a.txt"}}
    )
    assert len(log) == 1
    assert log[0]["tool_args"] == {"path": "a.txt"}
    assert log[0]["started_at"] == opened_at


def test_every_path_that_builds_a_log_gets_a_stamp():
    """Plan, automation and batch runs assemble entries by hand rather than via
    build_tool_call_event; they go through upsert_tool_call, which is why the
    stamp lives there."""
    log: list = []
    upsert_tool_call(log, {"tool_name": "bash", "tool_id": "call_9", "step_id": "s1"})
    assert isinstance(log[0]["started_at"], int)
    assert log[0]["step_id"] == "s1"


def test_closing_a_call_freezes_how_long_it_took():
    log: list = []
    upsert_tool_call(log, {"tool_name": "read_file", "tool_id": "call_1"})
    log[0]["started_at"] -= 4_000  # pretend the call ran for four seconds

    duration = attach_tool_result(log, "call_1", "read_file", {"ok": True})

    assert duration is not None and 4_000 <= duration < 5_000
    assert log[0]["duration_ms"] == duration
    assert log[0]["status"] == "success"


def test_the_result_event_carries_the_duration():
    log: list = []
    build_tool_call_event({"tool_name": "read_file", "tool_id": "call_1"}, "chat_1", log)
    log[0]["started_at"] -= 2_000

    evt = build_tool_result_event(
        {"tool_id": "call_1", "tool_name": "read_file", "result": {"ok": True}}, "chat_1", log
    )

    assert 2_000 <= evt["duration_ms"] < 3_000
    assert evt["duration_ms"] == log[0]["duration_ms"]


def test_a_result_without_its_call_reports_no_duration():
    """Nothing recorded the start, so there is no honest number to report —
    better an absent duration than an invented one."""
    log: list = []
    duration = attach_tool_result(log, "call_x", "read_file", {"ok": True})
    assert duration is None
    assert "duration_ms" not in log[0]
