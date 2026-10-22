"""Automation outcome bookkeeping and delivery, separate from live conversation execution."""

import asyncio
import time
from typing import Any, Dict

from core.infra.logging import get_logger
from core.infra.time import utc_now

logger = get_logger(__name__)


def _run_chat_id(run_id):
    from core.db.engine import SessionLocal
    from features.automation.models import ScheduledTaskRun

    with SessionLocal() as db:
        run = db.get(ScheduledTaskRun, run_id)
        return run.chat_id if run else None


async def execute_task(scheduler, task_id: str, user_id: str, *, resume_run_id=None):
    """Execute a single scheduled task."""
    from core.db.engine import SessionLocal
    from features.automation.service import AutomationService

    from .automation_conversation import (
        ConversationStopped,
        execute_loop,
        execute_plan,
        execute_prompt,
    )
    from .automation_delivery import deliver_result

    lease_token = scheduler._leases[task_id].token
    start = time.monotonic()

    with SessionLocal() as db:
        svc = AutomationService(db)
        task = svc.get_task_by_id(task_id)
        if not task or task.user_id != user_id:
            return
        if resume_run_id is None and task.status not in ("active", "paused"):
            return
        if resume_run_id is None:
            run = svc.record_run_start(task_id, lease_token=lease_token)
        else:
            from features.automation.models import ScheduledTaskRun

            run = db.get(ScheduledTaskRun, resume_run_id)
            if (
                run is None
                or run.task_id != task_id
                or run.status != "running"
                or run.lease_token != lease_token
            ):
                return
        task_type = task.task_type
        task_name = task.name or "定时任务"
        task_prompt = task.prompt
        task_plan_id = task.plan_id
        task_consecutive_failures = task.consecutive_failures or 0
        task_max_failures = task.max_failures or 3
        # Pass `None` (not `[]`) when the task wasn't configured with
        # explicit IDs so that plan-mode falls back to plan-declared
        # expected_* lists. An explicit empty list now means "strictly
        # no tools/skills/agents".
        enabled_mcp_ids = task.enabled_mcp_ids or None
        enabled_skill_ids = task.enabled_skill_ids or None
        enabled_kb_ids = task.enabled_kb_ids or None
        enabled_agent_ids = task.enabled_agent_ids or None
        task_metadata = dict(
            task.extra_data or {}
        )  # includes optional channel delivery destinations

    if resume_run_id is not None:
        from core.infra.time import as_utc

        start -= max(0, (utc_now() - as_utc(run.started_at)).total_seconds())

    try:
        from features.automation.execution import task_execution_context

        with SessionLocal() as db:
            current_task = AutomationService(db).get_task(task_id, user_id)
            if current_task is None:
                raise ValueError("任务已删除")
            task_execution_context(db, current_task)
        if resume_run_id is not None:
            from .automation_conversation import resume_conversation

            chat_id, result_text, usage = await resume_conversation(
                run.chat_id,
                user_id,
                run.started_at,
                automation_run_id=run.run_id,
                lease_token=lease_token,
            )
        elif task_type == "prompt":
            chat_id, result_text, usage = await execute_prompt(
                user_id=user_id,
                task_name=task_name,
                prompt=task_prompt,
                task_id=task_id,
                automation_run_id=run.run_id,
                lease_token=lease_token,
                enabled_mcp_ids=enabled_mcp_ids,
                enabled_skill_ids=enabled_skill_ids,
                enabled_kb_ids=enabled_kb_ids,
                enabled_agent_ids=enabled_agent_ids,
            )
        elif task_type == "plan":
            chat_id, result_text, usage = await execute_plan(
                user_id=user_id,
                task_name=task_name,
                plan_id=task_plan_id,
                task_id=task_id,
                automation_run_id=run.run_id,
                lease_token=lease_token,
                enabled_mcp_ids=enabled_mcp_ids,
                enabled_skill_ids=enabled_skill_ids,
                enabled_kb_ids=enabled_kb_ids,
                enabled_agent_ids=enabled_agent_ids,
            )
        elif task_type == "loop":
            # Periodically advance a persistent autonomous loop (M4 scheduler integration); loop_id stored in task.extra_data
            chat_id, result_text, usage = await execute_loop(
                user_id=user_id,
                task_name=task_name,
                loop_id=(task_metadata or {}).get("loop_id"),
                task_id=task_id,
                automation_run_id=run.run_id,
                lease_token=lease_token,
            )
        else:
            raise ValueError(f"Unknown task type: {task_type}")

        duration_ms = int((time.monotonic() - start) * 1000)

        still_exists = True
        with SessionLocal() as db:
            svc = AutomationService(db)
            # 任务可能在本次执行的几分钟里被用户删掉了（delete_task 是硬删）。
            # 下面的 update_task_system / finalize 对不存在的任务本就是空操作，
            # 但通知与渠道投递用的是开跑时抓下来的 task_name / task_metadata 快照，
            # 不查这一下就会给一个已经删掉的任务推「执行完成」。
            still_exists = svc.get_task_by_id(task_id) is not None
            completed = svc.record_run_complete(
                run.run_id,
                lease_token=lease_token,
                commit=False,
                status="success",
                chat_id=chat_id,
                # 存全文：这一份同时是渠道投递的正文来源，截断只发生在展示侧
                # （run_to_dict 的 summary_limit / 通知中心）。
                result_summary=result_text,
                duration_ms=duration_ms,
                usage=usage,
            )
            if completed is None:
                return
            svc.update_task_system(
                task_id,
                consecutive_failures=0,
                last_run_at=utc_now(),
                sidebar_activated=True,
            )
            # next_run_at already pre-advanced in _check_and_fire; now that
            # the run actually finished, flip one-shot/exhausted tasks to
            # "completed" (advance_next_run intentionally left them active so
            # this run could pass the execute_task guard).
            svc.finalize_after_run(task_id)

        logger.info("[scheduler] task %s completed in %dms", task_id, duration_ms)
        if still_exists:
            await deliver_result(
                scheduler,
                user_id,
                task_id,
                task_name,
                "success",
                result_text or "执行完成",
                chat_id,
                task_metadata,
            )

    except ConversationStopped as stopped:
        with SessionLocal() as db:
            svc = AutomationService(db)
            completed = svc.record_run_complete(
                run.run_id,
                lease_token=lease_token,
                commit=False,
                status="failed",
                chat_id=stopped.chat_id,
                error_message="用户已停止执行",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            if completed is None:
                return
            svc.update_task_system(task_id, last_run_at=utc_now(), sidebar_activated=True)
            svc.finalize_after_run(task_id)
        logger.info("[scheduler] task %s stopped by user", task_id)
    except asyncio.CancelledError:
        with SessionLocal() as db:
            svc = AutomationService(db)
            completed = svc.record_run_complete(
                run.run_id,
                lease_token=lease_token,
                commit=False,
                status="failed",
                chat_id=_run_chat_id(run.run_id),
                error_message="执行已中断",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            if completed is not None:
                db.commit()
                svc.finalize_after_run(task_id)
        raise
    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        error_msg = str(e)[:2000] or type(e).__name__
        logger.error("[scheduler] task %s failed: %s", task_id, error_msg, exc_info=True)

        still_exists = True
        with SessionLocal() as db:
            svc = AutomationService(db)
            still_exists = svc.get_task_by_id(task_id) is not None
            completed = svc.record_run_complete(
                run.run_id,
                lease_token=lease_token,
                commit=False,
                status="failed",
                chat_id=_run_chat_id(run.run_id),
                error_message=error_msg,
                duration_ms=duration_ms,
            )
            if completed is None:
                return
            new_failures = task_consecutive_failures + 1
            updates: Dict[str, Any] = {
                "consecutive_failures": new_failures,
                "last_error": error_msg,
                "last_run_at": utc_now(),
                "sidebar_activated": True,
            }
            if new_failures >= task_max_failures:
                updates["status"] = "disabled"
                logger.warning(
                    "[scheduler] task %s auto-disabled after %d failures", task_id, new_failures
                )
            svc.update_task_system(task_id, **updates)
            # next_run_at already pre-advanced in _check_and_fire. Finalize
            # one-shot tasks so a failed single run lands in a terminal state
            # instead of dangling active (finalize_after_run skips disabled).
            svc.finalize_after_run(task_id)

        if still_exists:
            await deliver_result(scheduler, user_id, task_id, task_name, "failed", error_msg[:200])
        else:
            logger.info(
                "[scheduler] task %s was deleted while running — skip failure notification",
                task_id,
            )
