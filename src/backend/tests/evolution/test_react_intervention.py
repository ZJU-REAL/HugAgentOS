"""Intervention rules reach the ReAct loop, not just the autonomous one.

The gap this closes: every other part of the orchestration profile shaped the
ReAct assembly — tools, skills, prompt fragments, routes, budgets — while the
intervention rules reached only ``autonomous_loop``, the separate long-running
product. That left the profile with one field governing nothing on the axis
nearly all traffic takes, which is a fair reading of "you are still tuning a
workflow".
"""

import asyncio
import types

import pytest

from agentscope.message import ToolResultState
from core.evolution.profiles.agent_profile_constants import ACTION_CHANGE_STRATEGY, SIGNAL_REPEATED_ACTIONS, SIGNAL_TOOL_ERROR_STREAK
from core.evolution.profiles.agent_profile import InterventionRule
from core.evolution.profiles.stall_hook import stall_hooks
from tests.orchestration.hook_support import make_adapter
from agentscope.tool._response import ToolResponse

def intervention_adapter(rules):
    return make_adapter(*stall_hooks(rules))


class _Ctx(list):
    pass


class _Agent:
    def __init__(self):
        self.state = types.SimpleNamespace(context=_Ctx(), reply_id="r1")


class _Call:
    def __init__(self, name, tool_input):
        self.name = name
        self.input = tool_input


class _Chunk:
    def __init__(self, state):
        self.state = state


def _run(mw, agent, call, state):
    async def handler(**_kw):
        yield ToolResponse(content=[], state=state)

    async def drive():
        async for _ in mw.on_acting(agent, {"tool_call": call}, handler):
            pass

    asyncio.run(drive())


def _reminders(agent):
    out = []
    for msg in agent.state.context:
        for block in getattr(msg, "content", []) or []:
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", "")
            if text and "system-reminder" in text:
                out.append(text)
    return out


def test_repeating_the_same_call_triggers_the_configured_action():
    mw = intervention_adapter(
        [InterventionRule(SIGNAL_REPEATED_ACTIONS, 2, ACTION_CHANGE_STRATEGY)]
    )
    agent = _Agent()
    call = _Call("search_web", {"q": "同一个查询"})
    for _ in range(3):
        _run(mw, agent, call, ToolResultState.SUCCESS)

    reminders = _reminders(agent)
    assert reminders, "the rule should have fired"
    assert "换一条思路" in reminders[0]
    # The reviewer can see which configured rule caused it.
    assert "repeated_actions" in reminders[0]


def test_a_different_call_breaks_the_streak():
    """A streak is consecutive by definition."""
    mw = intervention_adapter(
        [InterventionRule(SIGNAL_REPEATED_ACTIONS, 2, ACTION_CHANGE_STRATEGY)]
    )
    agent = _Agent()
    for tool in ("a", "b", "a", "b"):
        _run(mw, agent, _Agent() and _Call(tool, {}), ToolResultState.SUCCESS)
    assert _reminders(agent) == []


def test_consecutive_tool_failures_trigger_an_intervention():
    mw = intervention_adapter(
        [InterventionRule(SIGNAL_TOOL_ERROR_STREAK, 3, ACTION_CHANGE_STRATEGY)]
    )
    agent = _Agent()
    for i in range(3):
        _run(mw, agent, _Call(f"tool{i}", {}), ToolResultState.ERROR)
    assert _reminders(agent), "three consecutive failures should intervene"


def test_a_success_resets_the_failure_streak():
    mw = intervention_adapter(
        [InterventionRule(SIGNAL_TOOL_ERROR_STREAK, 3, ACTION_CHANGE_STRATEGY)]
    )
    agent = _Agent()
    _run(mw, agent, _Call("a", {}), ToolResultState.ERROR)
    _run(mw, agent, _Call("b", {}), ToolResultState.ERROR)
    _run(mw, agent, _Call("c", {}), ToolResultState.SUCCESS)
    _run(mw, agent, _Call("d", {}), ToolResultState.ERROR)
    assert _reminders(agent) == []


def test_the_same_intervention_is_not_repeated_every_iteration():
    """Repeating one reminder each turn makes it noise the model learns to skip."""
    mw = intervention_adapter(
        [InterventionRule(SIGNAL_REPEATED_ACTIONS, 2, ACTION_CHANGE_STRATEGY)]
    )
    agent = _Agent()
    call = _Call("search_web", {"q": "x"})
    for _ in range(8):
        _run(mw, agent, call, ToolResultState.SUCCESS)
    assert len(_reminders(agent)) == 1


def test_no_rules_means_no_behaviour_change():
    """The built-in profile must be a no-op until something is published."""
    mw = intervention_adapter([])
    agent = _Agent()
    call = _Call("search_web", {"q": "x"})
    for _ in range(5):
        _run(mw, agent, call, ToolResultState.ERROR)
    assert _reminders(agent) == []
