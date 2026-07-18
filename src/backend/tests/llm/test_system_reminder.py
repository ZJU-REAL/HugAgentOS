"""Tests for the system-reminder out-of-band channel."""

import pytest

from core.llm.system_reminder import (
    REMINDER_CLOSE,
    REMINDER_OPEN,
    inject_reminder,
    wrap_reminder,
)


class _FakeState:
    # AgentScope 2.0: the context is agent.state.context: list[Msg]
    def __init__(self):
        self.context = []


class _FakeAgent:
    def __init__(self):
        self.state = _FakeState()


def test_wrap_reminder_format():
    out = wrap_reminder("hello")
    assert out.startswith(REMINDER_OPEN + "\n")
    assert out.endswith("\n" + REMINDER_CLOSE)
    assert "hello" in out


def test_wrap_reminder_strips_outer_whitespace():
    assert wrap_reminder("   \n  body  \n   ") == f"{REMINDER_OPEN}\nbody\n{REMINDER_CLOSE}"


@pytest.mark.asyncio
async def test_inject_reminder_uses_user_role():
    """Key regression test: role must be 'user', not 'system' -- the latter triggers
    the model's defensive behavior of exiting the ReAct loop."""
    agent = _FakeAgent()
    assert await inject_reminder(agent, "hello") is True
    assert len(agent.state.context) == 1
    msg = agent.state.context[0]
    assert msg.role == "user", f"reminder role must be 'user', got '{msg.role}'"
    # 2.0: content is [TextBlock], take .text
    text = msg.content[0].text
    assert REMINDER_OPEN in text
    assert "hello" in text


@pytest.mark.asyncio
async def test_inject_reminder_empty_content_skipped():
    agent = _FakeAgent()
    assert await inject_reminder(agent, "") is False
    assert await inject_reminder(agent, "   \n  ") is False
    assert agent.state.context == []
