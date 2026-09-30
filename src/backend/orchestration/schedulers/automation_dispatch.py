"""Automation outcome bookkeeping and delivery, separate from live conversation execution."""

import asyncio
import time
from typing import Any, Dict
from core.infra.time import utc_now
from core.infra.logging import get_logger

logger = get_logger(__name__)


def _run_chat_id(run_id):
    from core.db.engine import SessionLocal
    from core.db.models import ScheduledTaskRun

    with SessionLocal() as db:
        run = db.get(ScheduledTaskRun, run_id)
        return run.chat_id if run else None


async def execute_task(scheduler, task_id: str, user_id: str, *, timeout: float):
    """Execute a single scheduled task."""
    from core.db.engine import SessionLocal
    from core.services.automation_service import AutomationService

    from .automation_artifacts import load_run_files
    from .automation_conversation import (
        ConversationStopped,
        execute_prompt,
        execute_plan,
        execute_loop,
    )

    start = time.monotonic()

    with SessionLocal() as db:
        svc = AutomationService(db)
        task = svc.get_task_by_id(task_id)
        if not task or task.user_id != user_id or task.status not in ("active", "paused"):
            return

        run = svc.record_run_start(task_id)
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

    try:
        from core.services.automation_execution import task_execution_context

        with SessionLocal() as db:
            current_task = AutomationService(db).get_task(task_id, user_id)
            if current_task is None:
                raise ValueError("任务已删除")
            task_execution_context(db, current_task)
        if task_type == "prompt":
            chat_id, result_text, usage = await asyncio.wait_for(
                execute_prompt(
                    user_id=user_id,
                    task_name=task_name,
                    prompt=task_prompt,
                    task_id=task_id,
                    automation_run_id=run.run_id,
                    enabled_mcp_ids=enabled_mcp_ids,
                    enabled_skill_ids=enabled_skill_ids,
                    enabled_kb_ids=enabled_kb_ids,
                    enabled_agent_ids=enabled_agent_ids,
                ),
                timeout=timeout,
            )
        elif task_type == "plan":
            chat_id, result_text, usage = await asyncio.wait_for(
                execute_plan(
                    user_id=user_id,
                    task_name=task_name,
                    plan_id=task_plan_id,
                    task_id=task_id,
                    automation_run_id=run.run_id,
                    enabled_mcp_ids=enabled_mcp_ids,
                    enabled_skill_ids=enabled_skill_ids,
                    enabled_kb_ids=enabled_kb_ids,
                    enabled_agent_ids=enabled_agent_ids,
                ),
                timeout=timeout,
            )
        elif task_type == "loop":
            # Periodically advance a persistent autonomous loop (M4 scheduler integration); loop_id stored in task.extra_data
            chat_id, result_text, usage = await asyncio.wait_for(
                execute_loop(
                    user_id=user_id,
                    task_name=task_name,
                    loop_id=(task_metadata or {}).get("loop_id"),
                    task_id=task_id,
                    automation_run_id=run.run_id,
                ),
                timeout=timeout,
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
            svc.record_run_complete(
                run.run_id,
                status="success",
                chat_id=chat_id,
                # 存全文：这一份同时是渠道投递的正文来源，截断只发生在展示侧
                # （run_to_dict 的 summary_limit / 通知中心）。
                result_summary=result_text,
                duration_ms=duration_ms,
                usage=usage,
            )
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

        # Multi-target delivery (delivery_targets model, with backward compat for the old flat channel_id/conversation_id).
        # In-app (notification center + sidebar + chat history) is delivered only when targets include inapp; channels and other outbound targets are delivered one by one.
        from core.services.delivery_targets import has_inapp, resolve_delivery_targets

        if not still_exists:
            logger.info(
                "[scheduler] task %s was deleted while running — skip notification/delivery",
                task_id,
            )
            return

        _targets = resolve_delivery_targets(task_metadata)
        if has_inapp(_targets):
            await scheduler._send_notification(
                user_id, task_id, task_name, "success", result_text or "执行完成", chat_id
            )
        logger.info("[scheduler] task %s completed in %dms", task_id, duration_ms)

        # Artifact files generated by this run (pinned deliverables anchored under this run's chat_id).
        # Loaded once and reused across all channel targets — otherwise "generate a document and send it to Feishu" only sends the text and the file never goes out.
        _gen_files = load_run_files(chat_id) if chat_id else []
        for _tgt in _targets:
            if _tgt.get("type") != "channel":
                continue  # inapp already handled; email etc. reserved
            _ch = _tgt.get("channel_id")
            _conv = _tgt.get("conversation_id")
            if not (_ch and _conv):
                continue
            try:
                from core.channels.outbound import deliver_to_conversation

                head = f"【{task_name}】\n" if task_name else ""
                _ok = await deliver_to_conversation(
                    _ch,
                    _conv,
                    head + (result_text or "执行完成"),
                    files=_gen_files,
                )
                if _ok:
                    logger.info(
                        "[scheduler] 渠道投递成功 task=%s channel=%s conv=%s files=%d",
                        task_id,
                        _ch,
                        _conv,
                        len(_gen_files),
                    )
                else:
                    logger.warning(
                        "[scheduler] 渠道投递失败（原因见 [channels] 日志）task=%s channel=%s conv=%s",
                        task_id,
                        _ch,
                        _conv,
                    )
            except Exception:
                logger.warning("[scheduler] 渠道投递异常 task=%s", task_id, exc_info=True)

    except ConversationStopped as stopped:
        with SessionLocal() as db:
            svc = AutomationService(db)
            svc.record_run_complete(
                run.run_id,
                status="failed",
                chat_id=stopped.chat_id,
                error_message="用户已停止执行",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            svc.update_task_system(task_id, last_run_at=utc_now(), sidebar_activated=True)
            svc.finalize_after_run(task_id)
        logger.info("[scheduler] task %s stopped by user", task_id)
    except asyncio.CancelledError:
        with SessionLocal() as db:
            svc = AutomationService(db)
            svc.record_run_complete(
                run.run_id,
                status="failed",
                chat_id=_run_chat_id(run.run_id),
                error_message="执行已中断",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            svc.finalize_after_run(task_id)
        raise
    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        error_msg = "定时任务执行超时" if isinstance(e, asyncio.TimeoutError) else str(e)[:2000]
        logger.error("[scheduler] task %s failed: %s", task_id, error_msg, exc_info=True)

        still_exists = True
        with SessionLocal() as db:
            svc = AutomationService(db)
            still_exists = svc.get_task_by_id(task_id) is not None
            svc.record_run_complete(
                run.run_id,
                status="failed",
                chat_id=_run_chat_id(run.run_id),
                error_message=error_msg,
                duration_ms=duration_ms,
            )
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
            await scheduler._send_notification(
                user_id, task_id, task_name, "failed", error_msg[:200]
            )
        else:
            logger.info(
                "[scheduler] task %s was deleted while running — skip failure notification",
                task_id,
            )
