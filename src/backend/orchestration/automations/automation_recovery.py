"""Take over a durable invocation without creating another run record."""
import asyncio
from core.db import engine as db_engine
from features.automation.models import ScheduledTaskRun
from features.automation.runs import AutomationRunRepository
from features.automation.service import AutomationService


def _runs(task_id):
    with db_engine.SessionLocal() as db:
        task = AutomationService(db).get_task_by_id(task_id)
        if task is None:
            return None, []
        return task.user_id, [
            (row.run_id, row.lease_token)
            for row in db.query(ScheduledTaskRun).filter(
                ScheduledTaskRun.task_id == task_id,
                ScheduledTaskRun.status.in_(("pending", "running")),
            ).order_by(ScheduledTaskRun.started_at).all()
        ]


def _claim(run_id, token, expected):
    with db_engine.SessionLocal() as db:
        return AutomationRunRepository(db).claim(run_id, token, expected_token=expected)


async def recover_task(scheduler, task_id):
    user_id, runs = await asyncio.to_thread(_runs, task_id)
    lease = scheduler._leases[task_id]
    for run_id, expected in runs:
        if not await lease.owns():
            return
        if not await asyncio.to_thread(_claim, run_id, lease.token, expected):
            continue
        if not await lease.owns():
            return
        from .automation_dispatch import execute_task
        await execute_task(scheduler, task_id, user_id, run_id=run_id)
