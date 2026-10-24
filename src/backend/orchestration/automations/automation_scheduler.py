"""Bounded dispatch of durable automation invocations."""
import asyncio
import contextlib
import uuid

from core.infra.logging import get_logger
from core.infra.time import utc_now

logger = get_logger(__name__)
POLL_INTERVAL_SECONDS = 3
MAX_EXECUTIONS = 8
REDIS_LOCK_PREFIX = "jx:auto:lock:"


class AutomationScheduler:
    def __init__(self):
        self._running = False
        self._task = None
        self._executions = set()
        self._leases = {}

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        executions = list(self._executions)
        for task in executions:
            task.cancel()
        await asyncio.gather(*executions, return_exceptions=True)
        for task_id in list(self._leases):
            await self._release_lock(task_id)

    @staticmethod
    def _pending():
        from core.db.engine import SessionLocal
        from features.automation.admission import admit, candidates

        with SessionLocal() as db:
            pending = candidates(db, MAX_EXECUTIONS)
            admit(db, max(0, MAX_EXECUTIONS - len(pending)))
            return candidates(db, MAX_EXECUTIONS)

    async def _poll_loop(self):
        while self._running:
            try:
                await self._dispatch_pending()
            except Exception:
                logger.exception("automation dispatch deferred")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _dispatch_pending(self):
        capacity = MAX_EXECUTIONS - len(self._executions)
        if capacity <= 0:
            return
        for task_id in await asyncio.to_thread(self._pending):
            if len(self._executions) >= MAX_EXECUTIONS:
                break
            if not await self._acquire_lock(task_id):
                continue
            task = asyncio.create_task(self.execute_task(task_id))
            self._executions.add(task)
            task.add_done_callback(self._executions.discard)

    async def execute_task(self, task_id):
        from .automation_recovery import recover_task

        try:
            await self._leases[task_id].run(recover_task(self, task_id))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("automation execution deferred task=%s", task_id)
        finally:
            await self._release_lock(task_id)

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

    async def _send_notification(
        self,
        user_id: str,
        task_id: str,
        task_name: str,
        status: str,
        summary: str,
        chat_id: str | None = None,
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
