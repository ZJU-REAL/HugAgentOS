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

import json

from agentscope.message import TextBlock, ToolResultState
from core.evolution.profiles.agent_profile_constants import ACTION_CHANGE_STRATEGY, ACTION_NARROW_SCOPE, SIGNAL_NO_PROGRESS, SIGNAL_REPEATED_ACTIONS, SIGNAL_TOOL_ERROR_STREAK
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


def _run(mw, agent, call, state, text=None):
    async def handler(**_kw):
        content = [TextBlock(type="text", text=text)] if text is not None else []
        yield ToolResponse(content=content, state=state)

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
        _run(mw, agent, call, ToolResultState.SUCCESS, "同样的结果")

    reminders = _reminders(agent)
    assert reminders, "the rule should have fired"
    assert "换一条思路" in reminders[0]
    # The model is told what it did, not which internal rule fired.
    assert "search_web" in reminders[0] and "同样的结果" in reminders[0]
    assert "repeated_actions" not in reminders[0]


def test_alternating_calls_whose_results_change_are_progress():
    mw = intervention_adapter(
        [InterventionRule(SIGNAL_REPEATED_ACTIONS, 2, ACTION_CHANGE_STRATEGY),
         InterventionRule(SIGNAL_NO_PROGRESS, 2, ACTION_CHANGE_STRATEGY)]
    )
    agent = _Agent()
    for step, tool in enumerate(("a", "b", "a", "b")):
        _run(mw, agent, _Call(tool, {}), ToolResultState.SUCCESS, f"result {step}")
    assert _reminders(agent) == []


def test_polling_a_status_that_changes_is_not_a_repeat():
    mw = intervention_adapter(
        [InterventionRule(SIGNAL_REPEATED_ACTIONS, 2, ACTION_CHANGE_STRATEGY)]
    )
    agent = _Agent()
    for percent in (10, 40, 70, 100):
        _run(mw, agent, _Call("status", {"job": "j1"}), ToolResultState.SUCCESS, f"{percent}%")
    assert _reminders(agent) == []


def _bash(stdout, task):
    """A Bash result as the tool returns it: a fresh task id and timing every call."""
    return json.dumps({"task_id": f"cmd_{task:032x}", "session_id": f"cmd_{task:032x}",
                       "status": "exited", "exit_code": 0, "stdout": stdout, "stderr": "",
                       "execution_time_ms": 100 + task, "error": ""})


def test_successful_commands_that_keep_printing_the_same_thing_are_no_progress():
    """The production loop: different edit scripts, exit code 0, the same length printed."""
    mw = intervention_adapter(
        [InterventionRule(SIGNAL_NO_PROGRESS, 2, ACTION_CHANGE_STRATEGY),
         InterventionRule(SIGNAL_REPEATED_ACTIONS, 3, ACTION_NARROW_SCOPE)]
    )
    agent = _Agent()
    scripts = [f"python3 -c \"t=open('brief.md').read(); t=t.replace('{n}', '{n}'); print(len(t))\""
               for n in ("甲", "乙", "丙")]
    for task in range(40):
        _run(mw, agent, _Call("Bash", {"command": scripts[task % 3]}),
             ToolResultState.SUCCESS, _bash("964\n", task))

    reminders = _reminders(agent)
    assert reminders, "identical output with a changing task id must still count as no progress"
    assert "Bash 的输出始终是 `964`" in reminders[0]
    assert "直接交付" in reminders[0]
    # Told again as the stall goes on, with current numbers, but not on every call.
    assert 2 <= len(reminders) <= 6
    assert reminders[0] != reminders[-1]


def test_a_new_result_ends_the_stall_and_restarts_the_schedule():
    mw = intervention_adapter(
        [InterventionRule(SIGNAL_NO_PROGRESS, 2, ACTION_CHANGE_STRATEGY)]
    )
    agent = _Agent()
    for task in range(3):
        _run(mw, agent, _Call("Bash", {"command": f"c{task}"}), ToolResultState.SUCCESS, _bash("964", task))
    assert len(_reminders(agent)) == 1
    _run(mw, agent, _Call("Bash", {"command": "wc"}), ToolResultState.SUCCESS, _bash("298", 9))
    for task in range(10, 13):
        _run(mw, agent, _Call("Bash", {"command": f"c{task}"}), ToolResultState.SUCCESS, _bash("298", task))
    reminders = _reminders(agent)
    assert len(reminders) == 2 and "`298`" in reminders[1]


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
    """Repeating one reminder each turn makes it noise the model learns to skip:
    it comes back only as the streak doubles."""
    mw = intervention_adapter(
        [InterventionRule(SIGNAL_REPEATED_ACTIONS, 2, ACTION_CHANGE_STRATEGY)]
    )
    agent = _Agent()
    call = _Call("search_web", {"q": "x"})
    for _ in range(8):
        _run(mw, agent, call, ToolResultState.SUCCESS, "same")
    reminders = _reminders(agent)
    assert len(reminders) == 2
    assert "最近 2 次" in reminders[0] and "最近 4 次" in reminders[1]


def test_no_rules_means_no_behaviour_change():
    """The built-in profile must be a no-op until something is published."""
    mw = intervention_adapter([])
    agent = _Agent()
    call = _Call("search_web", {"q": "x"})
    for _ in range(5):
        _run(mw, agent, call, ToolResultState.ERROR)
    assert _reminders(agent) == []
