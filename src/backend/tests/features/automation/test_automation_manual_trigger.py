"""Triggers and durable run acceptance commit or roll back together."""
import pytest
from datetime import timedelta
from sqlalchemy import event
from core.infra.time import utc_now
from features.automation.admission import admit
from features.automation.models import ScheduledTaskRun
from features.automation.service import AutomationService


def _task(db, **kw):
    svc = AutomationService(db)
    return svc, svc.create_task(user_id="u1", task_type="prompt", prompt="run",
                               cron_expression="30 10 29 6 *", **kw)


def test_manual_acceptance_is_durable_and_does_not_move_schedule(db_session):
    svc, task = _task(db_session)
    before = task.next_run_at
    svc.request_manual_trigger(task)
    svc.request_manual_trigger(task)
    ids = admit(db_session, 8)
    assert len(ids) == 1
    run = db_session.get(ScheduledTaskRun, ids[0])
    assert run.status == "pending"
    assert task.manual_trigger_at is None
    assert task.run_count == 1
    assert task.next_run_at == before
    assert admit(db_session, 8) == []


def test_busy_task_retains_manual_trigger_for_after_completion(db_session):
    svc, task = _task(db_session)
    svc.request_manual_trigger(task)
    run_id = admit(db_session, 8)[0]
    svc.request_manual_trigger(task)
    assert admit(db_session, 8) == []
    assert task.manual_trigger_at is not None
    db_session.get(ScheduledTaskRun, run_id).status = "success"
    db_session.commit()
    assert len(admit(db_session, 8)) == 1
    assert task.run_count == 2


def test_paused_task_manual_trigger_is_accepted(db_session):
    svc, task = _task(db_session)
    svc.pause_task(task.task_id, "u1")
    svc.request_manual_trigger(task)
    assert len(admit(db_session, 8)) == 1


@pytest.mark.parametrize("manual", [True, False])
def test_failed_acceptance_does_not_consume_trigger(db_session, manual):
    svc, task = _task(db_session, schedule_type="once")
    task.next_run_at = utc_now() - timedelta(seconds=5)
    if manual:
        svc.request_manual_trigger(task)
    db_session.commit()
    before = task.next_run_at
    def fail(*args):
        raise RuntimeError("storage failed")
    event.listen(db_session, "before_commit", fail)
    try:
        with pytest.raises(RuntimeError):
            admit(db_session, 8)
    finally:
        event.remove(db_session, "before_commit", fail)
        db_session.rollback()
    db_session.refresh(task)
    assert task.run_count == 0
    assert task.next_run_at == before
    assert bool(task.manual_trigger_at) == manual
    assert db_session.query(ScheduledTaskRun).count() == 0


def test_admission_is_bounded(db_session):
    for _ in range(4):
        svc, task = _task(db_session)
        svc.request_manual_trigger(task)
    assert len(admit(db_session, 2)) == 2
    assert len(admit(db_session, 2)) == 2
