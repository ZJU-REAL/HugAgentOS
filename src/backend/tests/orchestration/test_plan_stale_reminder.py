"""计划栏长期不动时，harness 软提醒一次；但绝不能变成每几轮就催。

两条历史都要防住：催得太少——线上实测一轮 20 次 LLM 往返的任务里 ``update_plan`` 只在
开头调了两次，用户全程盯着错的 1/5；催得太狠——间隔 3 轮加硬祈使语气，模型把"改计划"
当成了推进任务本身（chat_20260824_172543_s5ha46，47 次 update_plan 对 45 次真实工作）。
所以间隔取 10 轮、语气对齐 Claude Code 的"可忽略"表述。

锁住的不变量：
- 建过清单且还有未结算步骤时，停滞满 10 轮才回灌一次；
- **只催更、不催建**：没调用过 ``update_plan`` 就一个字都不发；
- 模型自己更新了清单，计时立刻清零；
- 清单全部结算完就不再打扰；
- 提醒文案必须是软表述，明确允许模型忽略。
"""

import json

import pytest

from core.llm.context.workspace_hooks import PlanStaleHook, plan_stale_hook
from core.harness.hooks import Invocation, HookStage
from core.llm.runtime.context_hook_adapter import append_hook_context
from tests.orchestration.hook_support import make_adapter
from types import SimpleNamespace

_PLAN_REMINDER_INTERVAL = PlanStaleHook().interval


class _FakeState:
    def __init__(self) -> None:
        self.user_message_text = "把这批问答记录做成 PPT"
        self.context: list = []


class _FakeAgent:
    def __init__(self) -> None:
        self.state = _FakeState()


def _Block(name, block_id, payload):
    return {"type": "tool_call", "name": name, "id": block_id, "input": payload}


def _ToolCallMsg(*blocks):
    return {"content": list(blocks)}


def _remind(policy, agent):
    context = [m.model_dump(mode="json") if hasattr(m, "model_dump") else m for m in agent.state.context]
    decision = policy(Invocation.create(run_id="plan-test", stage=HookStage.TRANSFORM_CONTEXT,
        operation_name="agent_reasoning", data={"context": context, "context_items": []}))
    append_hook_context(agent, SimpleNamespace(data=decision.patch))


def _plan(*statuses: str) -> dict:
    return {
        "title": "问答记录做成 PPT",
        "steps": [{"title": f"第 {i} 步", "status": s} for i, s in enumerate(statuses, start=1)],
    }


def _plan_call(agent: _FakeAgent, call_id: str, plan: dict, *, as_json: bool = False) -> None:
    payload = json.dumps(plan, ensure_ascii=False) if as_json else plan
    agent.state.context.append(_ToolCallMsg(_Block("update_plan", call_id, payload)))


def _reminders(agent: _FakeAgent) -> list[str]:
    out: list[str] = []
    for m in agent.state.context:
        content = getattr(m, "content", None)
        if isinstance(content, list) and content:
            text = getattr(content[0], "text", "")
            if "<system-reminder>" in text:
                out.append(text)
    return out


def _spin(mw: PlanStaleHook, agent: _FakeAgent, rounds: int) -> None:
    for _ in range(rounds):
        _remind(mw, agent)


def test_no_plan_never_reminds():
    """模型压根没建过计划 —— 催建不是这个中间件的活，一个字都不该发。"""
    mw = PlanStaleHook()
    agent = _FakeAgent()
    agent.state.context.append(_ToolCallMsg(_Block("bash", "t1", {"command": "ls"})))
    _spin(mw, agent, _PLAN_REMINDER_INTERVAL + 5)
    assert _reminders(agent) == []


def test_stale_plan_reminds_with_current_checklist():
    """停在 1/5 不动满 N 轮 → 回灌一次，且把真实进度和每一步状态都念出来。"""
    mw = PlanStaleHook()
    agent = _FakeAgent()
    _plan_call(
        agent,
        "call_1",
        _plan("completed", "in_progress", "pending", "pending", "pending"),
    )
    # 第一轮只是"看到"这份清单（计时起点），之后停滞满 interval 才催
    _spin(mw, agent, 1 + _PLAN_REMINDER_INTERVAL)
    rem = _reminders(agent)
    assert len(rem) == 1
    assert "1/5" in rem[0]
    assert "update_plan" in rem[0]
    assert "第 2 步" in rem[0]
    assert "in_progress" in rem[0]


def test_not_yet_stale_stays_quiet():
    """没到停滞阈值就不打扰 —— 每轮都催会把上下文塞满、也会打断模型手上的活。"""
    mw = PlanStaleHook()
    agent = _FakeAgent()
    _plan_call(agent, "call_1", _plan("in_progress", "pending"))
    _spin(mw, agent, _PLAN_REMINDER_INTERVAL)  # 含起点那一轮，还差一轮
    assert _reminders(agent) == []


def test_model_updating_the_plan_resets_the_clock():
    """模型真去更新了 —— 计时清零，不该紧接着再挨一次催。"""
    mw = PlanStaleHook()
    agent = _FakeAgent()
    _plan_call(agent, "call_1", _plan("in_progress", "pending", "pending"))
    _spin(mw, agent, _PLAN_REMINDER_INTERVAL)
    _plan_call(agent, "call_2", _plan("completed", "in_progress", "pending"))
    _spin(mw, agent, _PLAN_REMINDER_INTERVAL)
    assert _reminders(agent) == []
    _remind(mw, agent)
    assert len(_reminders(agent)) == 1


def test_finished_plan_is_left_alone():
    """全部结算完 —— 收尾过的清单不该再被催。"""
    mw = PlanStaleHook()
    agent = _FakeAgent()
    _plan_call(agent, "call_1", _plan("completed", "completed"))
    _spin(mw, agent, _PLAN_REMINDER_INTERVAL + 5)
    assert _reminders(agent) == []


def test_json_string_args_are_parsed():
    """有的供应商把 tool_call.input 当字符串回传 —— 解析不出来就等于永不催更。"""
    mw = PlanStaleHook()
    agent = _FakeAgent()
    _plan_call(agent, "call_1", _plan("in_progress", "pending"), as_json=True)
    _spin(mw, agent, 1 + _PLAN_REMINDER_INTERVAL)
    assert len(_reminders(agent)) == 1


def test_reminder_clock_restarts_after_each_nudge():
    """催过之后重新计时，不会连着两轮都催。"""
    mw = PlanStaleHook()
    agent = _FakeAgent()
    _plan_call(agent, "call_1", _plan("in_progress", "pending"))
    _spin(mw, agent, 1 + _PLAN_REMINDER_INTERVAL)
    assert len(_reminders(agent)) == 1
    _spin(mw, agent, _PLAN_REMINDER_INTERVAL - 1)
    assert len(_reminders(agent)) == 1
    _remind(mw, agent)
    assert len(_reminders(agent)) == 2


@pytest.mark.asyncio
async def test_on_reasoning_passes_events_through():
    """中间件只做旁路提醒，不能吞掉推理事件。"""
    mw = make_adapter(plan_stale_hook())
    agent = _FakeAgent()
    _plan_call(agent, "call_1", _plan("in_progress", "pending"))

    async def next_handler(**kwargs):
        yield "evt-1"
        yield "evt-2"

    seen = []
    for _ in range(1 + _PLAN_REMINDER_INTERVAL):
        async for evt in mw.on_reasoning(agent, {}, next_handler):
            seen.append(evt)
    assert seen == ["evt-1", "evt-2"] * (1 + _PLAN_REMINDER_INTERVAL)
    assert len(_reminders(agent)) == 1


def test_reminder_wording_stays_soft():
    """硬祈使会让模型把改计划当成推进任务——文案必须明确允许忽略、且不许打断手上的活。"""
    mw = PlanStaleHook()
    agent = _FakeAgent()
    _plan_call(agent, "call_1", _plan("in_progress", "pending"))
    _spin(mw, agent, 1 + _PLAN_REMINDER_INTERVAL)
    text = _reminders(agent)[0]
    assert "不适用就忽略" in text
    assert "仅在确实相关时才使用" in text
    assert "立刻调用" not in text


def test_interval_is_not_aggressive():
    """间隔一旦调回个位数，就会重演"拿改计划替代干活"的活锁。"""
    assert _PLAN_REMINDER_INTERVAL >= 10
