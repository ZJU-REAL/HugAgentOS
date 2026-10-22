"""Conditional ownership writes for scheduled execution records."""

import uuid

from core.infra.time import utc_now
from features.automation.models import ScheduledTaskRun


class AutomationRunRepository:
    def __init__(self, db):
        self.db = db

    def start(self, task_id, lease_token=None):
        run = ScheduledTaskRun(
            run_id=f"run_{uuid.uuid4().hex[:16]}",
            task_id=task_id,
            status="running",
            lease_token=lease_token,
            started_at=utc_now(),
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def claim(self, run_id, lease_token, *, expected_token):
        """Only a scheduler holding Redis ownership may transfer a running row."""
        if not lease_token:
            raise ValueError("Execution ownership token is required")
        count = (
            self.db.query(ScheduledTaskRun)
            .filter(
                ScheduledTaskRun.run_id == run_id,
                ScheduledTaskRun.status == "running",
                ScheduledTaskRun.lease_token == expected_token,
            )
            .update({ScheduledTaskRun.lease_token: lease_token}, synchronize_session=False)
        )
        self.db.commit()
        return count == 1

    def complete(self, run_id, *, lease_token, commit=True, **values):
        if not lease_token:
            raise ValueError("Execution ownership token is required")
        count = (
            self.db.query(ScheduledTaskRun)
            .filter(
                ScheduledTaskRun.run_id == run_id,
                ScheduledTaskRun.status == "running",
                ScheduledTaskRun.lease_token == lease_token,
            )
            .update({**values, "completed_at": utc_now()}, synchronize_session=False)
        )
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        if not count:
            return None
        self.db.expire_all()
        return self.db.get(ScheduledTaskRun, run_id)


def owned_run_guard(run_id, token):
    """Lock the automation owner in the journal's cancellation transaction."""
    if not token:
        raise ValueError("Execution ownership token is required")

    def guard(db):
        row = (
            db.query(ScheduledTaskRun)
            .filter(
                ScheduledTaskRun.run_id == run_id,
                ScheduledTaskRun.lease_token == token,
            )
            .with_for_update()
            .one_or_none()
        )
        return row is not None

    return guard
