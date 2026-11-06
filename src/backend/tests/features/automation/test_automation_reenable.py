"""Auto-disabled tasks can be re-enabled by their owner, with a fresh failure streak."""
from core.infra.time import utc_now
from features.automation.service import AutomationService


def _disabled_task(db, **kw):
    svc = AutomationService(db)
    task = svc.create_task(user_id="u1", task_type="prompt", prompt="run",
                           cron_expression="30 10 29 6 *", **kw)
    svc.update_task_system(task.task_id, status="disabled", consecutive_failures=3,
                           last_error="boom")
    return svc, task


def test_reenable_resets_failures_and_reschedules(db_session):
    svc, task = _disabled_task(db_session)
    resumed = svc.resume_task(task.task_id, "u1")
    assert resumed.status == "active"
    assert resumed.consecutive_failures == 0
    assert resumed.last_error is None
    assert resumed.next_run_at > utc_now()


def test_reenable_manual_task_stays_unscheduled(db_session):
    svc, task = _disabled_task(db_session, schedule_type="manual")
    resumed = svc.resume_task(task.task_id, "u1")
    assert resumed.status == "active"
    assert resumed.next_run_at is None


def test_reenable_is_owner_only_and_not_for_finished_tasks(db_session):
    svc, task = _disabled_task(db_session)
    assert svc.resume_task(task.task_id, "someone-else") is None
    svc.update_task_system(task.task_id, status="completed")
    assert svc.resume_task(task.task_id, "u1") is None


def test_pause_resume_keeps_failure_count(db_session):
    svc = AutomationService(db_session)
    task = svc.create_task(user_id="u1", task_type="prompt", prompt="run",
                           cron_expression="30 10 29 6 *")
    svc.update_task_system(task.task_id, consecutive_failures=2, last_error="boom")
    svc.pause_task(task.task_id, "u1")
    resumed = svc.resume_task(task.task_id, "u1")
    assert resumed.consecutive_failures == 2
    assert resumed.last_error == "boom"
