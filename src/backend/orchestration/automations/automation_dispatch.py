"""Automation outcome bookkeeping and delivery, separate from live conversation execution."""

import asyncio
import time
from typing import Any, Dict

from core.infra.logging import get_logger
from core.infra.time import utc_now
from core.db.threads import run_in_session

logger = get_logger(__name__)


def _run_chat_id(run_id):
    from core.db.engine import SessionLocal
    from features.automation.models import ScheduledTaskRun

    with SessionLocal() as db:
        run = db.get(ScheduledTaskRun, run_id)
        return run.chat_id if run else None


def _claimed(db, task_id, user_id, run_id, lease_token):
    """The leased run (detached) and a snapshot of its task, or None if the claim is stale."""
    from features.automation.models import ScheduledTaskRun
    from features.automation.service import AutomationService

    task = AutomationService(db).get_task_by_id(task_id)
    if not task or task.user_id != user_id:
        return None
    run = db.get(ScheduledTaskRun, run_id)
    if (
        run is None
        or run.task_id != task_id
        or run.status != "running"
        or run.lease_token != lease_token
    ):
        return None
    db.expunge(run)
    return run, {
        "task_type": task.task_type,
        "task_name": task.name or "定时任务",
        "prompt": task.prompt,
        "plan_id": task.plan_id,
        "consecutive_failures": task.consecutive_failures or 0,
        "max_failures": task.max_failures or 3,
        # Pass `None` (not `[]`) when the task wasn't configured with
        # explicit IDs so that plan-mode falls back to plan-declared
        # expected_* lists. An explicit empty list now means "strictly
        # no tools/skills/agents".
        "enabled_mcp_ids": task.enabled_mcp_ids or None,
        "enabled_skill_ids": task.enabled_skill_ids or None,
        "enabled_kb_ids": task.enabled_kb_ids or None,
        "enabled_agent_ids": task.enabled_agent_ids or None,
        "metadata": dict(task.extra_data or {}),
    }


def _record_success(db, run_id, task_id, *, lease_token, chat_id, result_text, duration_ms, usage):
    """Returns whether the task still exists, or None when the run was taken over."""
    from features.automation.service import AutomationService

    svc = AutomationService(db)
    # 任务可能在本次执行的几分钟里被用户删掉了（delete_task 是硬删）。
    # 下面的 update_task_system / finalize 对不存在的任务本就是空操作，
    # 但通知与渠道投递用的是开跑时抓下来的 task_name / task_metadata 快照，
    # 不查这一下就会给一个已经删掉的任务推「执行完成」。
    still_exists = svc.get_task_by_id(task_id) is not None
    completed = svc.record_run_complete(
        run_id,
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
        return None
    svc.update_task_system(
        task_id,
        consecutive_failures=0,
        last_run_at=utc_now(),
        sidebar_activated=True,
    )
    # Admission advanced the schedule; completion finalizes exhausted tasks.
    svc.finalize_after_run(task_id)
    return still_exists


def _record_stopped(db, run_id, task_id, *, lease_token, chat_id, duration_ms) -> bool:
    from features.automation.service import AutomationService

    svc = AutomationService(db)
    completed = svc.record_run_complete(
        run_id,
        lease_token=lease_token,
        commit=False,
        status="failed",
        chat_id=chat_id,
        error_message="用户已停止执行",
        duration_ms=duration_ms,
    )
    if completed is None:
        return False
    svc.update_task_system(task_id, last_run_at=utc_now(), sidebar_activated=True)
    svc.finalize_after_run(task_id)
    return True


def _record_failure(
    db, run_id, task_id, *, lease_token, error_msg, duration_ms, failures, max_failures
):
    """Returns whether the task still exists, or None when the run was taken over."""
    from features.automation.service import AutomationService

    svc = AutomationService(db)
    still_exists = svc.get_task_by_id(task_id) is not None
    completed = svc.record_run_complete(
        run_id,
        lease_token=lease_token,
        commit=False,
        status="failed",
        chat_id=_run_chat_id(run_id),
        error_message=error_msg,
        duration_ms=duration_ms,
    )
    if completed is None:
        return None
    updates: Dict[str, Any] = {
        "consecutive_failures": failures,
        "last_error": error_msg,
        "last_run_at": utc_now(),
        "sidebar_activated": True,
    }
    if failures >= max_failures:
        updates["status"] = "disabled"
        logger.warning("[scheduler] task %s auto-disabled after %d failures", task_id, failures)
    svc.update_task_system(task_id, **updates)
    # Finalize failed one-shot tasks without overriding auto-disable.
    svc.finalize_after_run(task_id)
    return still_exists


async def execute_task(scheduler, task_id: str, user_id: str, *, run_id: str):
    """Execute a single scheduled task."""
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

    claimed = await run_in_session(_claimed, task_id, user_id, run_id, lease_token)
    if claimed is None:
        return
    run, task = claimed
    task_type = task["task_type"]
    task_name = task["task_name"]
    task_consecutive_failures = task["consecutive_failures"]
    task_max_failures = task["max_failures"]
    enabled_mcp_ids = task["enabled_mcp_ids"]
    enabled_skill_ids = task["enabled_skill_ids"]
    enabled_kb_ids = task["enabled_kb_ids"]
    enabled_agent_ids = task["enabled_agent_ids"]
    task_metadata = task["metadata"]  # includes optional channel delivery destinations

    if run.chat_id:
        from core.infra.time import as_utc

        start -= max(0, (utc_now() - as_utc(run.started_at)).total_seconds())

    try:
        from features.automation.execution import task_execution_context

        def _check_task(db):
            current_task = AutomationService(db).get_task(task_id, user_id)
            if current_task is None:
                raise ValueError("任务已删除")
            task_execution_context(db, current_task)
        await run_in_session(_check_task)
        if run.chat_id:
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
                prompt=task["prompt"],
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
                plan_id=task["plan_id"],
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

        still_exists = await run_in_session(
            _record_success,
            run.run_id,
            task_id,
            lease_token=lease_token,
            chat_id=chat_id,
            result_text=result_text,
            duration_ms=duration_ms,
            usage=usage,
        )
        if still_exists is None:
            return

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
        stopped_recorded = await run_in_session(
            _record_stopped,
            run.run_id,
            task_id,
            lease_token=lease_token,
            chat_id=stopped.chat_id,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        if not stopped_recorded:
            return
        logger.info("[scheduler] task %s stopped by user", task_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        error_msg = str(e)[:2000] or type(e).__name__
        logger.error("[scheduler] task %s failed: %s", task_id, error_msg, exc_info=True)

        still_exists = await run_in_session(
            _record_failure,
            run.run_id,
            task_id,
            lease_token=lease_token,
            error_msg=error_msg,
            duration_ms=duration_ms,
            failures=task_consecutive_failures + 1,
            max_failures=task_max_failures,
        )
        if still_exists is None:
            return

        if still_exists:
            await deliver_result(scheduler, user_id, task_id, task_name, "failed", error_msg[:200])
        else:
            logger.info(
                "[scheduler] task %s was deleted while running — skip failure notification",
                task_id,
            )
