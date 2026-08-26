"""计划执行快照里的「已中断」位。

用户按下停止后，协作式取消让执行生成器正常收尾，落库这一步照样会走到。快照
不带中断位时，前端下次拉历史会把这张卡片重新渲染成执行态/执行完成 —— 用户看到
的就是"明明已经中断了，回来它又在跑（或者说自己跑完了）"。
"""

from types import SimpleNamespace

from core.services.plan_service import PlanService


def _plan(status: str):
    return SimpleNamespace(
        plan_id="plan_1",
        title="示例计划",
        description="",
        status=status,
        steps=[
            SimpleNamespace(
                step_order=1,
                title="第一步",
                description="",
                expected_tools=[],
                expected_skills=[],
                status="success",
                result_summary="ok",
                ai_output="ok",
                tool_calls_log=[],
            )
        ],
    )


def test_cancelled_plan_snapshot_carries_cancelled_flag():
    snap = PlanService.build_execution_snapshot(
        _plan("cancelled"), completed_steps=1, total_steps=3, result_text=""
    )
    assert snap["cancelled"] is True
    assert snap["mode"] == "complete"


def test_completed_plan_snapshot_is_not_marked_cancelled():
    snap = PlanService.build_execution_snapshot(
        _plan("completed"), completed_steps=3, total_steps=3, result_text="done"
    )
    assert snap["cancelled"] is False
