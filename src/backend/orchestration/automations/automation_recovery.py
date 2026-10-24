"""Recover automation outcomes by observing their durable child conversations."""

from core.db import engine as db_engine
from features.automation.models import ScheduledTaskRun
from core.infra.time import as_utc, utc_now
from features.automation.runs import AutomationRunRepository
from features.automation.service import AutomationService


async def recover_task(scheduler, task_id):
    # The caller renews task ownership while the shared ChatRun executor resumes
    # the durable child. Do not start a second child or abandon the old result.
    with db_engine.SessionLocal() as db:
        task = AutomationService(db).get_task_by_id(task_id)
        if task is None:
            return
        user_id = task.user_id
        runs = [
            (row.run_id, row.chat_id, row.lease_token)
            for row in db.query(ScheduledTaskRun)
            .filter(
                ScheduledTaskRun.task_id == task_id,
                ScheduledTaskRun.status == "running",
            )
            .all()
        ]
    lease = scheduler._leases[task_id]
    lease_token = lease.token
    for run_id, chat_id, expected_token in runs:
        if not await lease.owns():
            return
        with db_engine.SessionLocal() as db:
            if not AutomationRunRepository(db).claim(
                run_id, lease_token, expected_token=expected_token
            ):
                continue
            claimed = db.get(ScheduledTaskRun, run_id)
            if claimed is None or claimed.status != "running" or claimed.lease_token != lease_token:
                continue
            # The old owner may have linked its child after our first snapshot
            # but before the conditional claim. Observe the claimed row's link.
            chat_id = claimed.chat_id
        if not await lease.owns():
            return
        if chat_id:
            from .automation_dispatch import execute_task

            await execute_task(scheduler, task_id, user_id, resume_run_id=run_id)
        else:
            # No conversation was ever linked; there is no child to resume.
            with db_engine.SessionLocal() as db:
                service = AutomationService(db)
                if (
                    service.record_run_complete(
                        run_id,
                        lease_token=lease_token,
                        status="failed",
                        error_message="interrupted (orphan, no linked conversation)",
                    )
                    is None
                ):
                    continue
                task = service.get_task_by_id(task_id)
                if task and task.next_run_at and as_utc(task.next_run_at) <= utc_now():
                    service.advance_next_run(task_id)
                service.finalize_after_run(task_id)
