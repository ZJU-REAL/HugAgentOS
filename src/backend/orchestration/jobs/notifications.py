"""Best-effort conversation progress and wake notifications."""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _emit_progress(chat_id: Optional[str], note: str) -> None:
    """活性信号：转译成 model_progress 喂主 run 的无活动看门狗。

    长作业期间主对话没有任何可渲染输出，缺了这条会被 600s 看门狗当成卡死强杀。

    ⚠️ 它**只喂看门狗**：``sub_type="progress"`` 在 workflow 里被折叠成 model_progress，
    而 model_progress 不写流。要让用户看见进度得走下面的 ``_emit_ui_progress``。
    """
    if not chat_id:
        return
    try:
        from core.llm import _subagent_stream

        if not _subagent_stream.is_active(chat_id):
            return
        _subagent_stream.push(
            chat_id,
            {"sub_type": "progress", "agent_id": "job", "agent_name": "批量作业", "note": note},
        )
    except Exception:  # noqa: BLE001 —— 活性信号是尽力而为，永远不该拖垮作业
        pass


def _emit_ui_progress(
    chat_id: Optional[str], job_row_id: str, name: str, stats: Dict[str, Any]
) -> None:
    """把作业台账的实时数字打到主对话的 run_job 工具卡上。

    为什么必须单独有这条：``wait=True`` 的作业会把主对话在 run_job 里阻塞几十分钟，
    这期间模型没有任何输出、工具也没有新调用——前端于是停在「run_job 运行中」转圈，
    步骤条一个数都不动，用户只能靠刷新页面才发现其实早跑完了（实测 54 分钟一轮）。
    活性信号解决的是「后端别把它当卡死杀掉」，这条解决的是「用户看得见它在动」。

    走 subagent_event 旁路：``sub_type`` 不是 ``progress`` 就会被 workflow 原样下发、
    被 chat_run_executor 写进 run 的 Redis 流（断线续播也能重放）；``attach_subagent_step``
    不认这个 sub_type，所以**不落**持久化工具日志——刷新后由 tool_result 说明结局，
    不留一行过期的中途进度。

    ``parent_tool_id`` 取自 ActingToolCallIdMiddleware 写好的 ContextVar：驱动跑在
    run_job 这次工具调用的同一条任务链上（``wait=False`` 的后台 task 也继承了这份
    上下文副本），所以拿到的就是该工具卡的 id，前端据此把进度贴到正确的卡片上。
    """
    if not chat_id:
        return
    try:
        from core.llm import _subagent_stream

        if not _subagent_stream.is_active(chat_id):
            return
        try:
            from core.llm.middlewares import CURRENT_TOOL_CALL_ID

            parent_tool_id = CURRENT_TOOL_CALL_ID.get("") or ""
        except Exception:  # noqa: BLE001
            parent_tool_id = ""
        total = int(stats.get("total", 0) or 0)
        settled = int(stats.get("settled", 0) or 0)
        _subagent_stream.push(
            chat_id,
            {
                "sub_type": "job_progress",
                "parent_tool_id": parent_tool_id,
                "parent_tool_name": "run_job",
                "agent_id": "job",
                "agent_name": "批量作业",
                "job_id": job_row_id,
                "job_name": name or "",
                "total": total,
                "settled": settled,
                "done": int(stats.get("done", 0) or 0),
                "failed": int(stats.get("failed", 0) or 0),
                "not_found": int(stats.get("not_found", 0) or 0),
                "running": int(stats.get("running", 0) or 0),
                "pending": int(stats.get("pending", 0) or 0),
            },
        )
    except Exception:  # noqa: BLE001 —— 进度是尽力而为，永远不该拖垮作业
        pass


async def _maybe_wake(job_row_id: str) -> None:
    """作业终态后叫醒会话（幂等；失败只记日志，不影响作业结果）。"""
    try:
        from orchestration.job_wakeup import wake_on_job_finish

        await wake_on_job_finish(job_row_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[job] wake failed job=%s: %s", job_row_id, exc)


async def _maybe_wake_progress(
    job_row_id: str, *, stats: Dict[str, Any], budget_left: Dict[str, Any], stalled: bool
) -> None:
    """中途播报进度（失败只记日志，绝不影响作业本身）。"""
    try:
        from orchestration.job_wakeup import wake_on_job_progress

        await wake_on_job_progress(
            job_row_id, stats=stats, budget_left=budget_left, stalled=stalled
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[job] progress wake failed job=%s: %s", job_row_id, exc)
