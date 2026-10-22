"""Automation scheduler — polls the DB for due tasks and fires them.

Inspired by claude-code's CronScheduler:
- Polls every 15s for due tasks
- Uses Redis distributed lock to prevent double-firing across instances
- Handles missed tasks on startup
- Auto-disables tasks after consecutive failure threshold
- Writes notifications to Redis for frontend polling
"""

import asyncio
import contextlib
import random
import uuid
from typing import List, Optional, Tuple

from core.infra.background import spawn
from core.infra.logging import get_logger
from core.infra.time import utc_now

logger = get_logger(__name__)


POLL_INTERVAL_SECONDS = 15
# 手动触发要比 cron 轮询响应快得多——按钮叫「立即执行」，等 15 秒不叫立即。一拍就是
# 一条 UPDATE，所以这一档只取到「点下去像是立刻有反应」为止，不必更密。
MANUAL_DRAIN_INTERVAL_SECONDS = 3
REDIS_LOCK_PREFIX = "jx:auto:lock:"


class AutomationScheduler:
    """Async scheduler that polls the DB for due tasks and fires them."""

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._manual_task: Optional[asyncio.Task] = None
        self._executions: set[asyncio.Task] = set()
        self._leases = {}

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
        # A queued execution can be cancelled before its coroutine enters its
        # finally block. Its already-started renewal must still stop with us.
        for task_id in list(self._leases):
            await self._release_lock(task_id)
        logger.info("[scheduler] stopped")

    async def _poll_loop(self):
        # Small initial delay to let the app finish startup
        await asyncio.sleep(5)

        await self._recover_stuck_running_runs()
        # Then handle missed one-shot tasks
        await self._recover_missed_tasks()

        while self._running:
            try:
                await self._recover_stuck_running_runs()
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
        from features.automation.service import AutomationService

        with SessionLocal() as db:
            return AutomationService(db).take_manual_triggers()

    async def _check_and_fire(self):
        from core.db.engine import SessionLocal
        from features.automation.service import AutomationService

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
        from features.automation.service import AutomationService

        with SessionLocal() as db:
            AutomationService(db).advance_next_run(task_id)

    @staticmethod
    def _bump_run_count(task_id: str) -> None:
        # run_count 只在 advance_next_run 里 +1，而手动触发不走那条路，所以单独补一次。
        from core.db.engine import SessionLocal
        from features.automation.service import AutomationService

        with SessionLocal() as db:
            AutomationService(db).bump_run_count(task_id)

    async def _fire(self, task_id: str, user_id: str, kind: str, pre_run) -> None:
        """取锁 → 跑 pre_run 记账 → 后台执行。定时与手动触发共用，所以两者对同一个任务
        不会并发跑；pre_run 失败不拦截执行，理由同 _check_and_fire 的 pre-advance。"""
        if not await self._acquire_lock(task_id):
            logger.info("[scheduler] %s for %s skipped: already running", kind, task_id)
            return
        try:
            if await asyncio.to_thread(self._has_running_runs, task_id):
                self._launch_recovery(task_id)
                return
            await asyncio.to_thread(pre_run, task_id)
        except asyncio.CancelledError:
            await self._release_lock(task_id)
            raise
        except Exception as exc:
            logger.warning(
                "[scheduler] pre-run bookkeeping failed for %s: %s — firing anyway", task_id, exc
            )
        logger.info("[scheduler] firing %s for %s", kind, task_id)
        self._launch_execution(task_id, user_id, lease=self._leases[task_id])

    def _launch_execution(self, task_id: str, user_id: str, *, lease=None) -> asyncio.Task:
        execution = spawn(
            self.execute_task(task_id, user_id, lease=lease), name=f"automation.{task_id}"
        )
        self._executions.add(execution)
        execution.add_done_callback(self._executions.discard)
        return execution

    async def execute_task(self, task_id: str, user_id: str, *, lease=None):
        from .automation_dispatch import execute_task

        if lease is None:
            if not await self._acquire_lock(task_id):
                return
            lease = self._leases[task_id]
        try:
            if await asyncio.to_thread(self._has_running_runs, task_id):
                from .automation_recovery import recover_task

                await lease.run(recover_task(self, task_id))
            else:
                await lease.run(execute_task(self, task_id, user_id))
        finally:
            await self._release_lock(task_id)

    @staticmethod
    def _has_running_runs(task_id):
        from core.db.engine import SessionLocal
        from features.automation.models import ScheduledTaskRun

        with SessionLocal() as db:
            return (
                db.query(ScheduledTaskRun.run_id)
                .filter(
                    ScheduledTaskRun.task_id == task_id,
                    ScheduledTaskRun.status == "running",
                )
                .first()
                is not None
            )

    def _launch_recovery(self, task_id):
        recovery = spawn(self._recover_task(task_id), name=f"automation.recover.{task_id}")
        self._executions.add(recovery)
        recovery.add_done_callback(self._executions.discard)

    async def _recover_stuck_running_runs(self):
        """Recover abandoned runs only after acquiring their execution ownership.

        Elapsed runtime cannot identify an orphan: a healthy task may run for
        days. A live worker's renewable lease excludes recovery on every poll.
        """
        from core.db.engine import SessionLocal
        from features.automation.models import ScheduledTaskRun

        try:
            with SessionLocal() as db:
                task_ids = [
                    row[0]
                    for row in db.query(ScheduledTaskRun.task_id)
                    .filter(ScheduledTaskRun.status == "running")
                    .distinct()
                    .all()
                ]
            for task_id in task_ids:
                if not await self._acquire_lock(task_id):
                    continue
                self._launch_recovery(task_id)
        except Exception as exc:
            logger.error("[scheduler] orphan recovery failed: %s", exc, exc_info=True)

    async def _recover_task(self, task_id):
        from .automation_recovery import recover_task

        try:
            await self._leases[task_id].run(recover_task(self, task_id))
        except Exception as exc:
            logger.error(
                "[scheduler] orphan recovery failed for %s: %s", task_id, exc, exc_info=True
            )
        finally:
            await self._release_lock(task_id)

    async def _recover_missed_tasks(self):
        """On startup, check for one-shot tasks whose next_run_at is in the past."""
        from core.db.engine import SessionLocal
        from features.automation.service import AutomationService

        now = utc_now()
        try:
            with SessionLocal() as db:
                svc = AutomationService(db)
                missed = svc.get_due_tasks(now)
                one_shot_count = 0
                for task in missed:
                    if not task.recurring:
                        one_shot_count += 1
                        await self._fire(
                            task.task_id, task.user_id, "missed task", self._advance_next_run
                        )
                if one_shot_count:
                    logger.info("[scheduler] recovering %d missed one-shot tasks", one_shot_count)
        except Exception as e:
            logger.error("[scheduler] recovery error: %s", e)

    # ── Redis lock helpers ─────────────────────────────────────────

    async def _acquire_lock(self, task_id: str) -> bool:
        from .automation_lease import AutomationLease

        if task_id in self._leases:
            return False
        try:
            lease = AutomationLease(f"{REDIS_LOCK_PREFIX}{task_id}")
            if not await lease.acquire():
                return False
            self._leases[task_id] = lease
            return True
        except Exception as e:
            logger.warning("[scheduler] lock acquire failed for %s: %s", task_id, e)
            return False

    async def _release_lock(self, task_id: str):
        lease = self._leases.pop(task_id, None)
        if lease is None:
            return
        try:
            await lease.release()
        except Exception as e:
            logger.error("[scheduler] lock release failed for %s: %s", task_id, e)

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
            from features.automation import notifications as notifications
            from features.automation.service import SUMMARY_LIMIT_BRIEF, truncate_summary

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
