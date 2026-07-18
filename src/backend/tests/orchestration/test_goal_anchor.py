"""Tests for GoalAnchorReminderMiddleware (periodic original-prompt reminder).

AgentScope 2.0: the former ``make_goal_anchor_reminder_hook`` is now
``core.llm.middlewares.GoalAnchorReminderMiddleware``. Semantic differences (vs the 1.x hook):
  - Counting unit changed from "per tool_call" to "per on_reasoning turn"; no more skip-tools filtering.
  - Output-tool hits are now detected by scanning the ``tool_call`` blocks at the tail of
    ``agent.state.context`` (rather than the 1.x kwargs.tool_call).
The trigger logic (warmup gate / interval / force once on the first output-tool) stays the same.
"""

import pytest

from core.llm.hooks import _GOAL_ANCHOR_INTERVAL, _GOAL_ANCHOR_WARMUP_CALLS
from core.llm.middlewares import GoalAnchorReminderMiddleware


class _FakeState:
    # AgentScope 2.0: the middleware reads agent.state.user_message_text, writes agent.state.context
    def __init__(self, user_message_text: str = "original user request"):
        self.user_message_text = user_message_text
        self.context: list = []


class _FakeAgent:
    def __init__(self, user_message_text: str = "original user request"):
        self.state = _FakeState(user_message_text)


class _ToolCallMsg:
    """Fake a history message carrying a tool_call block, for the output-tool detection scan."""
    def __init__(self, *names: str):
        self._blocks = [type("_B", (), {"name": n})() for n in names]

    def get_content_blocks(self, block_type: str):
        return self._blocks if block_type == "tool_call" else []


def _reminders(agent) -> list[str]:
    """Extract the <system-reminder> text appended by the middleware into context (ignoring the faked tool messages)."""
    out: list[str] = []
    for m in agent.state.context:
        content = getattr(m, "content", None)
        if isinstance(content, list) and content:
            text = getattr(content[0], "text", "")
            if "<system-reminder>" in text:
                out.append(text)
    return out


@pytest.mark.asyncio
async def test_no_fire_before_interval_and_no_output_tool():
    """With no output-tool, nothing fires before the interval is reached."""
    mw = GoalAnchorReminderMiddleware()
    agent = _FakeAgent()
    for _ in range(_GOAL_ANCHOR_INTERVAL - 1):
        mw._maybe_remind(agent)
    assert _reminders(agent) == []


@pytest.mark.asyncio
async def test_interval_fires():
    """With no output-tool, reaching the full interval fires once."""
    mw = GoalAnchorReminderMiddleware()
    agent = _FakeAgent()
    for _ in range(_GOAL_ANCHOR_INTERVAL):
        mw._maybe_remind(agent)
    assert len(_reminders(agent)) == 1


@pytest.mark.asyncio
async def test_output_tool_fires_at_warmup():
    """If there is an output-tool call in context at the moment warmup is reached, fire once as output_tool."""
    mw = GoalAnchorReminderMiddleware()
    agent = _FakeAgent()
    agent.state.context.append(_ToolCallMsg("bash"))
    for _ in range(_GOAL_ANCHOR_WARMUP_CALLS):
        mw._maybe_remind(agent)
    rem = _reminders(agent)
    assert len(rem) == 1
    assert "original user request" in rem[0]
    assert "<system-reminder>" in rem[0]


@pytest.mark.asyncio
async def test_output_tool_only_fires_once_per_turn():
    """After warmup, continuous output-tool hits fire only once (output_seen lock)."""
    mw = GoalAnchorReminderMiddleware()
    agent = _FakeAgent()
    agent.state.context.append(_ToolCallMsg("bash"))
    for _ in range(_GOAL_ANCHOR_WARMUP_CALLS):
        mw._maybe_remind(agent)
    assert len(_reminders(agent)) == 1
    for _ in range(3):
        mw._maybe_remind(agent)
    assert len(_reminders(agent)) == 1


@pytest.mark.asyncio
async def test_non_output_tools_do_not_trigger():
    """Delivery/input-direction tools are not in the output set — do not trigger early as output_tool
    (empirically verified 2026-05-26: re-injecting a reminder into the delivery path drops the pin rate 92% → 0%)."""
    mw = GoalAnchorReminderMiddleware()
    agent = _FakeAgent()
    agent.state.context.append(
        _ToolCallMsg("sandbox_get_artifact", "pin_to_workspace", "sandbox_put_artifact")
    )
    for _ in range(_GOAL_ANCHOR_WARMUP_CALLS + 1):  # still not at the interval
        mw._maybe_remind(agent)
    assert _reminders(agent) == []


@pytest.mark.asyncio
async def test_no_user_message_text_skips():
    """Never fire when user_message_text is empty."""
    mw = GoalAnchorReminderMiddleware()
    agent = _FakeAgent(user_message_text="")
    agent.state.context.append(_ToolCallMsg("bash"))
    for _ in range(_GOAL_ANCHOR_INTERVAL + 5):
        mw._maybe_remind(agent)
    assert _reminders(agent) == []


@pytest.mark.asyncio
async def test_batch_mode_returns_noop():
    """When batch_mode=True, on_reasoning does not call _maybe_remind."""
    mw = GoalAnchorReminderMiddleware(batch_mode=True)
    agent = _FakeAgent()
    agent.state.context.append(_ToolCallMsg("bash"))

    async def next_handler(**kwargs):
        for ev in ():
            yield ev

    for _ in range(20):
        async for _ in mw.on_reasoning(agent, {}, next_handler):
            pass
    assert _reminders(agent) == []


@pytest.mark.asyncio
async def test_interval_resets_after_each_reminder():
    """After each reminder _since_last resets to zero, and the next interval counts afresh."""
    mw = GoalAnchorReminderMiddleware()
    agent = _FakeAgent()
    for _ in range(_GOAL_ANCHOR_INTERVAL):
        mw._maybe_remind(agent)
    assert len(_reminders(agent)) == 1
    for _ in range(_GOAL_ANCHOR_INTERVAL - 1):
        mw._maybe_remind(agent)
    assert len(_reminders(agent)) == 1
    mw._maybe_remind(agent)
    assert len(_reminders(agent)) == 2
