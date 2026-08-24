"""计划栏不能停在半路 —— 模型忘了更新时，harness 要把清单塞回它眼前。

线上实测（2026-08-24，本地 chat_20260824_053729_8bo1ce）：一轮任务跑了 20 次 LLM 往返、
产出两个 PPTX、正常给出最终答复，日志里 ``[update_plan]`` 只在开头出现两次、都停在 1/5。
原因是清单的推进 100% 靠模型自觉：``update_plan`` 的用法只在系统提示词开头讲一次，之后
十几轮工具结果早把它冲淡了。workflow 末尾那道兜底只补"没有 pending、只剩一个 in_progress"
的收尾场景，对"半路就不动了"的清单无能为力，于是用户全程盯着一个错的 1/5。

所以这里锁住催更中间件的四条不变量：
- 模型建过清单、并且清单还有没结算的步骤时，停滞满 N 轮就把当前清单原样回灌一次；
- **只催更、不催建**：从没调用过 ``update_plan`` 就一个字都不发（简单问答不该被打扰）；
- 模型真去更新了（出现新的 ``update_plan`` 调用），计时立刻清零；
- 清单全部结算完就不再打扰。
"""

import json

import pytest

from core.llm.hooks import _PLAN_STALE_INTERVAL
from core.llm.middlewares import PlanStaleReminderMiddleware


class _FakeState:
    def __init__(self) -> None:
        self.user_message_text = "把这批问答记录做成 PPT"
        self.context: list = []


class _FakeAgent:
    def __init__(self) -> None:
        self.state = _FakeState()


class _Block:
    def __init__(self, name: str, block_id: str, payload) -> None:
        self.name = name
        self.id = block_id
        self.input = payload


class _ToolCallMsg:
    """伪造一条带 tool_call 块的历史消息。"""

    def __init__(self, *blocks: _Block) -> None:
        self._blocks = list(blocks)

    def has_content_blocks(self, block_type: str) -> bool:
        return block_type == "tool_call" and bool(self._blocks)

    def get_content_blocks(self, block_type: str):
        return self._blocks if block_type == "tool_call" else []


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


def _spin(mw: PlanStaleReminderMiddleware, agent: _FakeAgent, rounds: int) -> None:
    for _ in range(rounds):
        mw._maybe_remind(agent)


def test_no_plan_never_reminds():
    """模型压根没建过计划 —— 催建不是这个中间件的活，一个字都不该发。"""
    mw = PlanStaleReminderMiddleware()
    agent = _FakeAgent()
    agent.state.context.append(_ToolCallMsg(_Block("bash", "t1", {"command": "ls"})))
    _spin(mw, agent, _PLAN_STALE_INTERVAL + 5)
    assert _reminders(agent) == []


def test_stale_plan_reminds_with_current_checklist():
    """停在 1/5 不动满 N 轮 → 回灌一次，且把真实进度和每一步状态都念出来。"""
    mw = PlanStaleReminderMiddleware()
    agent = _FakeAgent()
    _plan_call(
        agent,
        "call_1",
        _plan("completed", "in_progress", "pending", "pending", "pending"),
    )
    # 第一轮只是"看到"这份清单（计时起点），之后停滞满 interval 才催
    _spin(mw, agent, 1 + _PLAN_STALE_INTERVAL)
    rem = _reminders(agent)
    assert len(rem) == 1
    assert "1/5" in rem[0]
    assert "update_plan" in rem[0]
    assert "第 2 步" in rem[0]
    assert "in_progress" in rem[0]


def test_not_yet_stale_stays_quiet():
    """没到停滞阈值就不打扰 —— 每轮都催会把上下文塞满、也会打断模型手上的活。"""
    mw = PlanStaleReminderMiddleware()
    agent = _FakeAgent()
    _plan_call(agent, "call_1", _plan("in_progress", "pending"))
    _spin(mw, agent, _PLAN_STALE_INTERVAL)  # 含起点那一轮，还差一轮
    assert _reminders(agent) == []


def test_model_updating_the_plan_resets_the_clock():
    """模型真去更新了 —— 计时清零，不该紧接着再挨一次催。"""
    mw = PlanStaleReminderMiddleware()
    agent = _FakeAgent()
    _plan_call(agent, "call_1", _plan("in_progress", "pending", "pending"))
    _spin(mw, agent, _PLAN_STALE_INTERVAL)
    _plan_call(agent, "call_2", _plan("completed", "in_progress", "pending"))
    _spin(mw, agent, _PLAN_STALE_INTERVAL)
    assert _reminders(agent) == []
    mw._maybe_remind(agent)
    assert len(_reminders(agent)) == 1


def test_finished_plan_is_left_alone():
    """全部结算完 —— 收尾过的清单不该再被催。"""
    mw = PlanStaleReminderMiddleware()
    agent = _FakeAgent()
    _plan_call(agent, "call_1", _plan("completed", "completed"))
    _spin(mw, agent, _PLAN_STALE_INTERVAL + 5)
    assert _reminders(agent) == []


def test_json_string_args_are_parsed():
    """有的供应商把 tool_call.input 当字符串回传 —— 解析不出来就等于永不催更。"""
    mw = PlanStaleReminderMiddleware()
    agent = _FakeAgent()
    _plan_call(agent, "call_1", _plan("in_progress", "pending"), as_json=True)
    _spin(mw, agent, 1 + _PLAN_STALE_INTERVAL)
    assert len(_reminders(agent)) == 1


def test_reminder_clock_restarts_after_each_nudge():
    """催过之后重新计时，不会连着两轮都催。"""
    mw = PlanStaleReminderMiddleware()
    agent = _FakeAgent()
    _plan_call(agent, "call_1", _plan("in_progress", "pending"))
    _spin(mw, agent, 1 + _PLAN_STALE_INTERVAL)
    assert len(_reminders(agent)) == 1
    _spin(mw, agent, _PLAN_STALE_INTERVAL - 1)
    assert len(_reminders(agent)) == 1
    mw._maybe_remind(agent)
    assert len(_reminders(agent)) == 2


@pytest.mark.asyncio
async def test_on_reasoning_passes_events_through():
    """中间件只做旁路提醒，不能吞掉推理事件。"""
    mw = PlanStaleReminderMiddleware()
    agent = _FakeAgent()
    _plan_call(agent, "call_1", _plan("in_progress", "pending"))

    async def next_handler(**kwargs):
        yield "evt-1"
        yield "evt-2"

    seen = []
    for _ in range(1 + _PLAN_STALE_INTERVAL):
        async for evt in mw.on_reasoning(agent, {}, next_handler):
            seen.append(evt)
    assert seen == ["evt-1", "evt-2"] * (1 + _PLAN_STALE_INTERVAL)
    assert len(_reminders(agent)) == 1
