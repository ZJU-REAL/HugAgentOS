"""批量作业的进度必须真的到得了前端的工具卡。

背景（线上实测）：一次 `run_job(wait=True)` 把主对话在同一个工具里阻塞了 54 分钟。
这期间驱动确实每 5 秒发一次进度，但它走的是 `sub_type="progress"` —— workflow 把这个
sub_type 折叠成 `model_progress`，而 `model_progress` 明确**不写流**（它只喂无活动
看门狗）。于是前端整整 54 分钟收不到任何可渲染事件：工具步骤条停在"执行中 · N 个步骤"
转圈，用户只能靠刷新页面才发现后台其实早跑完了。

所以这里锁两条不变量：

1. 给用户看的那帧进度**不能**用会被吞掉的 `sub_type="progress"`，且要带上分子分母；
2. 它只活在实时流里 —— `attach_subagent_step` 必须原样放过，不落持久化工具日志
   （否则刷新后卡片上会留一行过期数字，还会把展示名污染成"批量作业"）。
"""

from typing import Any, Dict, List

import pytest
from core.chat.tool_log import attach_subagent_step


@pytest.fixture()
def pushed(monkeypatch) -> List[Dict[str, Any]]:
    """截获 _subagent_stream 旁路上真正被推出去的事件。"""
    from core.llm import _subagent_stream

    events: List[Dict[str, Any]] = []
    monkeypatch.setattr(_subagent_stream, "is_active", lambda chat_id: True)
    monkeypatch.setattr(
        _subagent_stream, "push", lambda chat_id, payload: events.append(payload)
    )
    return events


def test_ui_progress_carries_numbers_and_dodges_the_swallowed_sub_type(pushed):
    from orchestration import job_runtime

    job_runtime._emit_ui_progress(
        "chat_1",
        "job_1",
        "补全展品",
        {"total": 568, "settled": 128, "done": 120, "failed": 3, "not_found": 5,
         "running": 8, "pending": 432},
    )

    assert len(pushed) == 1
    ev = pushed[0]
    # 用 "progress" 就会在 workflow 里被折叠成 model_progress、永远到不了前端
    assert ev["sub_type"] == "job_progress"
    assert (ev["total"], ev["settled"], ev["failed"]) == (568, 128, 3)
    # 前端据此把进度贴到 run_job 那张卡上（拿不到 tool_call_id 时按工具名回退）
    assert ev["parent_tool_name"] == "run_job"


def test_ui_progress_binds_to_the_current_tool_call(pushed):
    from core.llm.middlewares import CURRENT_TOOL_CALL_ID
    from orchestration import job_runtime

    token = CURRENT_TOOL_CALL_ID.set("call_abc")
    try:
        job_runtime._emit_ui_progress("chat_1", "job_1", "", {"total": 10, "settled": 1})
    finally:
        CURRENT_TOOL_CALL_ID.reset(token)

    assert pushed[0]["parent_tool_id"] == "call_abc"


def test_ui_progress_silent_without_an_active_stream(monkeypatch):
    """后台作业跑到主对话已经收尾之后：没有监听者就什么都别推。"""
    from core.llm import _subagent_stream
    from orchestration import job_runtime

    pushes: List[Any] = []
    monkeypatch.setattr(_subagent_stream, "is_active", lambda chat_id: False)
    monkeypatch.setattr(_subagent_stream, "push", lambda *a, **k: pushes.append(a))

    job_runtime._emit_ui_progress("chat_1", "job_1", "", {"total": 10, "settled": 1})
    job_runtime._emit_ui_progress(None, "job_1", "", {"total": 10, "settled": 1})

    assert pushes == []


def test_ui_progress_is_not_persisted_into_the_tool_log():
    """中途进度不进持久化日志：刷新后由 tool_result 说明结局，不留过期数字。"""
    log = [{"tool_id": "call_abc", "tool_name": "run_job"}]
    attach_subagent_step(
        log,
        "call_abc",
        {
            "sub_type": "job_progress",
            "agent_name": "批量作业",
            "total": 568,
            "settled": 128,
        },
    )
    assert log == [{"tool_id": "call_abc", "tool_name": "run_job"}]
