"""One-shot acceptance records work before removing the schedule."""
from datetime import timedelta
from core.infra.time import utc_now
from features.automation.admission import admit
from features.automation.models import ScheduledTaskRun
from features.automation.service import AutomationService


def _accept(db, *, schedule_type="once", max_runs=None):
    svc = AutomationService(db)
    task = svc.create_task(user_id="u1", task_type="prompt", prompt="run",
                          cron_expression="30 10 29 6 *",
                          schedule_type=schedule_type, max_runs=max_runs)
    task.next_run_at = utc_now() - timedelta(seconds=5)
    db.commit()
    run_id = admit(db, 1)[0]
    return svc, task, db.get(ScheduledTaskRun, run_id)


def test_oneshot_is_pending_after_schedule_is_consumed(db_session):
    svc, task, run = _accept(db_session)
    assert task.status == "active"
    assert task.next_run_at is None
    assert task.run_count == 1
    assert run.status == "pending"
    assert admit(db_session, 1) == []
    run.status = "success"
    db_session.commit()
    svc.finalize_after_run(task.task_id)
    assert task.status == "completed"


def test_recurring_continues_after_completed_invocation(db_session):
    svc, task, run = _accept(db_session, schedule_type="recurring")
    assert task.next_run_at > utc_now()
    run.status = "success"
    db_session.commit()
    svc.finalize_after_run(task.task_id)
    assert task.status == "active"


def test_max_runs_and_disabled_are_respected(db_session):
    svc, task, run = _accept(db_session, schedule_type="recurring", max_runs=1)
    assert task.next_run_at is None
    svc.update_task_system(task.task_id, status="disabled")
    run.status = "failed"
    db_session.commit()
    svc.finalize_after_run(task.task_id)
    assert task.status == "disabled"
