"""Automation scheduler — polls the DB for due tasks and fires them.

Inspired by claude-code's CronScheduler:
- Polls every 15s for due tasks
- Uses Redis distributed lock to prevent double-firing across instances
- Handles missed tasks on startup
- Auto-disables tasks after consecutive failure threshold
- Writes notifications to Redis for frontend polling
"""

from core.infra.time import utc_now

import asyncio
import contextlib
import random
import uuid
from typing import List, Optional, Tuple

from core.infra.background import spawn
from core.infra.logging import get_logger

logger = get_logger(__name__)


POLL_INTERVAL_SECONDS = 15
# 手动触发要比 cron 轮询响应快得多——按钮叫「立即执行」，等 15 秒不叫立即。一拍就是
# 一条 UPDATE，所以这一档只取到「点下去像是立刻有反应」为止，不必更密。
MANUAL_DRAIN_INTERVAL_SECONDS = 3
REDIS_LOCK_PREFIX = "jx:auto:lock:"
REDIS_LOCK_TTL = 1800  # 30 minutes max lock hold (> TASK_EXECUTION_TIMEOUT_S)
# Max wall-clock per task execution. Must be < REDIS_LOCK_TTL so the
# timeout fires before the lock expires (otherwise scheduler could
# fire a parallel run while the previous one is still running).
# Heavy tasks (multi-domain search + full Word generation) legitimately
# run ~13 min and were getting killed at the old 800s ceiling, so this
# is raised to 25 min.
TASK_EXECUTION_TIMEOUT_S = 1500
# Runs older than this in 'running' state on startup are treated as
# orphaned (killed by OOM/restart) and recovered to 'failed'. Kept above
# TASK_EXECUTION_TIMEOUT_S so a live run is never falsely recovered.
STUCK_RUNNING_THRESHOLD_S = 2400  # 40 minutes


class AutomationScheduler:
    """Async scheduler that polls the DB for due tasks and fires them."""

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._manual_task: Optional[asyncio.Task] = None
        self._executions: set[asyncio.Task] = set()

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        self._manual_task = asyncio.create_task(self._manual_drain_loop())
        logger.info("[scheduler] started")

    async def stop(self):
        self._running = False
        for task in (self._task, self._manual_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        executions = list(self._executions)
        for execution in executions:
            execution.cancel()
        if executions:
            await asyncio.gather(*executions, return_exceptions=True)
        logger.info("[scheduler] stopped")

    async def _poll_loop(self):
        # Small initial delay to let the app finish startup
        await asyncio.sleep(5)

        # Recover stuck 'running' rows from previous OOM/restart first
        await self._recover_stuck_running_runs()
        # Then handle missed one-shot tasks
        await self._recover_missed_tasks()

        while self._running:
            try:
                await self._check_and_fire()
            except Exception as e:
                logger.error("[scheduler] poll error: %s", e, exc_info=True)
            jitter = random.uniform(0, 5)
            await asyncio.sleep(POLL_INTERVAL_SECONDS + jitter)

    async def _manual_drain_loop(self):
        """执行用户点下的「立即执行」，节奏见 MANUAL_DRAIN_INTERVAL_SECONDS。"""
        await asyncio.sleep(5)
        while self._running:
            try:
                await self._fire_manual_triggers()
            except Exception as e:
                logger.error("[scheduler] manual drain error: %s", e, exc_info=True)
            await asyncio.sleep(MANUAL_DRAIN_INTERVAL_SECONDS)

    async def _fire_manual_triggers(self):
        # 每 3 秒一次的同步 DB 调用不能压在事件循环上——这个 worker 的 SSE 流和它抢同
        # 一个连接池，池满时 checkout 会把整个循环卡住 DB_POOL_TIMEOUT 那么久。
        pending = await asyncio.to_thread(self._take_manual_triggers)
        for task_id, user_id in pending:
            # 手动触发不推进 next_run_at——用户按一次不该打乱 cron 排期。
            await self._fire(task_id, user_id, "manual trigger", self._bump_run_count)

    @staticmethod
    def _take_manual_triggers() -> List[Tuple[str, str]]:
        from core.db.engine import SessionLocal
        from core.services.automation_service import AutomationService

        with SessionLocal() as db:
            return AutomationService(db).take_manual_triggers()

    async def _check_and_fire(self):
        from core.db.engine import SessionLocal
        from core.services.automation_service import AutomationService

        now = utc_now()
        with SessionLocal() as db:
            svc = AutomationService(db)
            due_tasks = svc.get_due_tasks(now)

        if not due_tasks:
            return

        logger.info("[scheduler] found %d due tasks", len(due_tasks))
        for task in due_tasks:
            # Pre-advance next_run_at BEFORE firing so the schedule moves
            # on regardless of whether the run itself succeeds, fails, or
            # gets killed mid-flight. Mirrors how real cron behaves and
            # prevents the death-spiral where a stuck "running" row leaves
            # next_run_at in the past forever and causes every poll to
            # re-fire the same task. The success/failure branches in
            # execute_task no longer call advance_next_run.
            await self._fire(task.task_id, task.user_id, "due task", self._advance_next_run)

    @staticmethod
    def _advance_next_run(task_id: str) -> None:
        from core.db.engine import SessionLocal
        from core.services.automation_service import AutomationService

        with SessionLocal() as db:
            AutomationService(db).advance_next_run(task_id)

    @staticmethod
    def _bump_run_count(task_id: str) -> None:
        # run_count 只在 advance_next_run 里 +1，而手动触发不走那条路，所以单独补一次。
        from core.db.engine import SessionLocal
        from core.services.automation_service import AutomationService

        with SessionLocal() as db:
            AutomationService(db).bump_run_count(task_id)

    async def _fire(self, task_id: str, user_id: str, kind: str, pre_run) -> None:
        """取锁 → 跑 pre_run 记账 → 后台执行。定时与手动触发共用，所以两者对同一个任务
        不会并发跑；pre_run 失败不拦截执行，理由同 _check_and_fire 的 pre-advance。"""
        if not await self._acquire_lock(task_id):
            logger.info("[scheduler] %s for %s skipped: already running", kind, task_id)
            return
        try:
            await asyncio.to_thread(pre_run, task_id)
        except Exception as exc:
            logger.warning(
                "[scheduler] pre-run bookkeeping failed for %s: %s — firing anyway", task_id, exc
            )
        logger.info("[scheduler] firing %s for %s", kind, task_id)
        self._launch_execution(task_id, user_id)

    def _launch_execution(self, task_id: str, user_id: str) -> asyncio.Task:
        execution = spawn(self.execute_task(task_id, user_id), name=f"automation.{task_id}")
        self._executions.add(execution)
        execution.add_done_callback(self._executions.discard)
        return execution

    async def execute_task(self, task_id: str, user_id: str):
        from .automation_dispatch import execute_task

        try:
            await execute_task(self, task_id, user_id, timeout=TASK_EXECUTION_TIMEOUT_S)
        finally:
            await self._release_lock(task_id)

    async def _recover_stuck_running_runs(self):
        """Mark long-running 'running' runs as failed (orphaned by OOM/restart).

        Without this, the DB accumulates rows in 'running' state from
        runs that were killed mid-flight, and (worse) the parent task's
        next_run_at can stay in the past — causing every poll to immediately
        re-fire the same task in a death spiral.
        """
        from datetime import datetime, timedelta, timezone

        from core.db.engine import SessionLocal
        from core.db.models import ScheduledTaskRun

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=STUCK_RUNNING_THRESHOLD_S)
        try:
            with SessionLocal() as db:
                stuck = (
                    db.query(ScheduledTaskRun)
                    .filter(
                        ScheduledTaskRun.status == "running",
                        ScheduledTaskRun.started_at < cutoff,
                    )
                    .all()
                )
                stuck_task_ids = set()
                for run in stuck:
                    run.status = "failed"
                    run.error_message = "interrupted (orphan, recovered on startup)"
                    run.completed_at = datetime.now(timezone.utc)
                    stuck_task_ids.add(run.task_id)

                # Push parent tasks' next_run_at past now so the next poll
                # doesn't immediately re-fire the same hung task.
                from core.services.automation_service import AutomationService

                svc = AutomationService(db)
                for tid in stuck_task_ids:
                    task = svc.get_task_by_id(tid)
                    if task and task.next_run_at:
                        try:
                            svc.advance_next_run(tid)
                        except Exception as exc:
                            logger.warning(
                                "[scheduler] startup advance failed for %s: %s",
                                tid,
                                exc,
                            )
                    # One-shot tasks pre-advance to next_run_at=None, so the
                    # branch above skips them; finalize so an interrupted single
                    # run doesn't dangle 'active' with no next_run_at forever.
                    try:
                        svc.finalize_after_run(tid)
                    except Exception as exc:
                        logger.warning(
                            "[scheduler] startup finalize failed for %s: %s",
                            tid,
                            exc,
                        )

                if stuck:
                    db.commit()
                    logger.info(
                        "[scheduler] recovered %d stuck 'running' runs across %d tasks",
                        len(stuck),
                        len(stuck_task_ids),
                    )
        except Exception as exc:
            logger.error("[scheduler] stuck-runs recovery failed: %s", exc, exc_info=True)

    async def _recover_missed_tasks(self):
        """On startup, check for one-shot tasks whose next_run_at is in the past."""
        from core.db.engine import SessionLocal
        from core.services.automation_service import AutomationService

        now = utc_now()
        try:
            with SessionLocal() as db:
                svc = AutomationService(db)
                missed = svc.get_due_tasks(now)
                one_shot_count = 0
                for task in missed:
                    if not task.recurring:
                        one_shot_count += 1
                        self._launch_execution(task.task_id, task.user_id)
                if one_shot_count:
                    logger.info("[scheduler] recovering %d missed one-shot tasks", one_shot_count)
        except Exception as e:
            logger.error("[scheduler] recovery error: %s", e)

    # ── Redis lock helpers ─────────────────────────────────────────

    async def _acquire_lock(self, task_id: str) -> bool:
        try:
            from core.infra.ephemeral import get_ephemeral_state

            key = f"{REDIS_LOCK_PREFIX}{task_id}"
            return await get_ephemeral_state().claim(key, ttl=REDIS_LOCK_TTL)
        except Exception as e:
            logger.warning("[scheduler] lock acquire failed for %s: %s", task_id, e)
            return False

    async def _release_lock(self, task_id: str):
        try:
            from core.infra.ephemeral import get_ephemeral_state

            await get_ephemeral_state().drop(f"{REDIS_LOCK_PREFIX}{task_id}")
        except Exception as e:
            logger.warning("[scheduler] lock release failed for %s: %s", task_id, e)

    # ── Notification ───────────────────────────────────────────────

    async def _send_notification(
        self,
        user_id: str,
        task_id: str,
        task_name: str,
        status: str,
        summary: str,
        chat_id: Optional[str] = None,
    ):
        try:
            from core.services import automation_notifications as notifications
            from core.services.automation_service import SUMMARY_LIMIT_BRIEF, truncate_summary

            notification = {
                "id": f"notif_{uuid.uuid4().hex[:12]}",
                "task_id": task_id,
                "task_name": task_name,
                "status": status,
                # 走统一截断策略：result_summary 现在是全文，裸切会把整篇报告拦腰截断且
                # 不带任何提示，与运行历史列表的观感不一致。
                "summary": truncate_summary(summary, SUMMARY_LIMIT_BRIEF),
                "chat_id": chat_id,
                "timestamp": int(utc_now().timestamp() * 1000),
                "read": False,
            }
            await notifications.push(user_id, notification)
        except Exception as e:
            logger.warning("[scheduler] notification failed: %s", e)
