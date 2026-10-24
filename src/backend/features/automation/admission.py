"""Durable admission: consume a trigger and insert its run in one transaction."""
import uuid

from sqlalchemy import and_, or_

from core.infra.time import utc_now
from features.automation.models import ScheduledTask, ScheduledTaskRun
from features.automation import scheduling

LIVE = ("pending", "running")


def admit(db, limit):
    if limit <= 0:
        return []
    now = utc_now()
    outstanding = db.query(ScheduledTaskRun.run_id).filter(
        ScheduledTaskRun.task_id == ScheduledTask.task_id,
        ScheduledTaskRun.status.in_(LIVE),
    ).exists()
    tasks = (
        db.query(ScheduledTask)
        .filter(
            ScheduledTask.status.in_(("active", "paused", "completed")),
            ~outstanding,
            or_(
                ScheduledTask.manual_trigger_at.isnot(None),
                and_(ScheduledTask.status == "active",
                     ScheduledTask.schedule_type != "manual",
                     ScheduledTask.next_run_at <= now),
            ),
        )
        .order_by(ScheduledTask.updated_at, ScheduledTask.task_id)
        .with_for_update(skip_locked=True)
        .limit(limit).all()
    )
    ids = []
    for task in tasks:
        manual = task.manual_trigger_at is not None
        count = (task.run_count or 0) + 1
        values = {"run_count": count, "updated_at": now}
        if manual:
            values["manual_trigger_at"] = None
        elif (not task.recurring or task.schedule_type == "once"
              or (task.max_runs and count >= task.max_runs)):
            values["next_run_at"] = None
        else:
            values["next_run_at"] = scheduling.compute_next_run(task.cron_expression, task.timezone)
        # Row locks are an optimization; the conditional write also fences
        # SQLite and a contender whose initial SELECT observed an older row.
        changed = db.query(ScheduledTask).filter(
            ScheduledTask.task_id == task.task_id,
            ScheduledTask.run_count == task.run_count,
            ScheduledTask.manual_trigger_at == task.manual_trigger_at,
            ScheduledTask.next_run_at == task.next_run_at,
            ScheduledTask.updated_at == task.updated_at,
            ~outstanding,
        ).update(values, synchronize_session=False)
        if changed != 1:
            continue
        run = ScheduledTaskRun(
            run_id="run_" + uuid.uuid4().hex[:16],
            task_id=task.task_id, status="pending", started_at=now,
        )
        db.add(run)
        ids.append(run.run_id)
    db.commit()
    db.expire_all()
    return ids


def candidates(db, limit):
    return [
        task_id
        for (task_id,) in db.query(ScheduledTask.task_id)
        .join(ScheduledTaskRun)
        .filter(ScheduledTaskRun.status.in_(LIVE))
        .order_by(ScheduledTaskRun.started_at)
        .limit(limit).all()
    ]
