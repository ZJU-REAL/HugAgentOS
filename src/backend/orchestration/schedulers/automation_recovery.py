"""Recover automation outcomes by observing their durable child conversations."""

from core.db import engine as db_engine
from core.db.models import ScheduledTaskRun
from core.infra.time import as_utc, utc_now
from core.services.automation_service import AutomationService


async def recover_task(scheduler, task_id):
    # The caller renews task ownership while the shared ChatRun executor resumes
    # the durable child. Do not start a second child or abandon the old result.
    with db_engine.SessionLocal() as db:
        task = AutomationService(db).get_task_by_id(task_id)
        if task is None:
            return
        user_id = task.user_id
        runs = [
            (row.run_id, row.chat_id)
            for row in db.query(ScheduledTaskRun)
            .filter(
                ScheduledTaskRun.task_id == task_id,
                ScheduledTaskRun.status == "running",
            )
            .all()
        ]
    for run_id, chat_id in runs:
        if chat_id:
            from .automation_dispatch import execute_task

            await execute_task(scheduler, task_id, user_id, resume_run_id=run_id)
        else:
            # No conversation was ever linked; there is no child to resume.
            with db_engine.SessionLocal() as db:
                run = db.get(ScheduledTaskRun, run_id)
                if run is None or run.status != "running":
                    continue
                run.status = "failed"
                run.error_message = "interrupted (orphan, no linked conversation)"
                run.completed_at = utc_now()
                db.commit()
                service = AutomationService(db)
                task = service.get_task_by_id(task_id)
                if task and task.next_run_at and as_utc(task.next_run_at) <= utc_now():
                    service.advance_next_run(task_id)
                service.finalize_after_run(task_id)
