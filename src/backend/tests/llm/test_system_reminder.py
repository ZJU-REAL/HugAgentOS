"""Tests for the shared <system-reminder> wrapper."""

from core.harness.context_items import REMINDER_CLOSE, REMINDER_OPEN, reminder_item, wrap_reminder


def test_wrap_reminder_format():
    out = wrap_reminder("hello")
    assert out.startswith(REMINDER_OPEN + "\n")
    assert out.endswith("\n" + REMINDER_CLOSE)
    assert "hello" in out


def test_wrap_reminder_strips_outer_whitespace():
    assert wrap_reminder("   \n  body  \n   ") == f"{REMINDER_OPEN}\nbody\n{REMINDER_CLOSE}"


def test_reminder_item_is_a_system_trusted_reminder():
    item = reminder_item("hello", "harness:test")
    assert item["content"] == wrap_reminder("hello")
    assert (item["kind"], item["origin"], item["trust"]) == ("reminder", "harness:test", "system")
