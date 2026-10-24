"""Construct interrupted execution state for ownership/fault-injection tests."""
from uuid import uuid4
from core.infra.time import utc_now
from features.automation.models import ScheduledTaskRun


def running_run(db, task_id, lease_token=None):
    run = ScheduledTaskRun(
        run_id="run_" + uuid4().hex[:16], task_id=task_id,
        status="running", lease_token=lease_token, started_at=utc_now(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run
