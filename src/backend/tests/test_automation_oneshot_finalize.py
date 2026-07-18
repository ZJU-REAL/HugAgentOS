"""Regression: one-shot scheduled tasks must actually execute (and deliver).

Bug: the scheduler pre-advances a task (run_count++, schedule moved on) BEFORE
firing execute_task, to avoid a death-spiral on crashed runs. For one-shot
tasks the pre-advance used to set status="completed". But execute_task's first
guard refuses any task whose status isn't active/paused — so the executor
skipped the very run it had just fired: no run record, no last_run_at, and the
channel delivery (Feishu, etc.) at the end never ran. The task *looked* executed
(run_count=1, status=completed) while its body never ran.

Fix: advance_next_run only clears next_run_at for one-shot tasks (stops
re-firing); terminal "completed" status is set AFTER the run via
finalize_after_run().
"""

from core.services.automation_service import AutomationService


def _task(db, **kw):
    svc = AutomationService(db)
    return svc, svc.create_task(
        user_id="u1",
        task_type="prompt",
        prompt="发送一条测试消息",
        cron_expression="30 10 29 6 *",
        **kw,
    )


def test_oneshot_pre_advance_keeps_task_runnable(db_session):
    """After pre-advance a one-shot task must stay active (so execute_task's
    guard passes) yet have next_run_at cleared (so it is never re-fired)."""
    svc, task = _task(db_session, schedule_type="once")
    assert task.status == "active"

    svc.advance_next_run(task.task_id)
    db_session.refresh(task)

    # The exact pair the old code got wrong:
    assert task.status == "active", "pre-advance must NOT mark one-shot completed"
    assert task.next_run_at is None, "pre-advance must clear next_run_at"
    assert task.run_count == 1

    # ...and the guard execute_task uses would now let it run.
    assert task.status in ("active", "paused")

    # get_due_tasks must NOT re-select it (next_run_at is NULL).
    from datetime import datetime, timezone
    due = svc.get_due_tasks(datetime.now(timezone.utc))
    assert task.task_id not in {t.task_id for t in due}


def test_finalize_marks_oneshot_completed_after_run(db_session):
    svc, task = _task(db_session, schedule_type="once")
    svc.advance_next_run(task.task_id)

    svc.finalize_after_run(task.task_id)
    db_session.refresh(task)

    assert task.status == "completed"
    assert task.next_run_at is None


def test_finalize_leaves_recurring_active(db_session):
    svc, task = _task(db_session, schedule_type="recurring")
    svc.advance_next_run(task.task_id)
    db_session.refresh(task)
    # recurring: schedule moved forward, still active
    assert task.status == "active"
    assert task.next_run_at is not None

    svc.finalize_after_run(task.task_id)
    db_session.refresh(task)
    assert task.status == "active", "recurring task must stay active after a run"
    assert task.next_run_at is not None


def test_finalize_completes_when_max_runs_reached(db_session):
    svc, task = _task(db_session, schedule_type="recurring", max_runs=2)
    # first run
    svc.advance_next_run(task.task_id)
    svc.finalize_after_run(task.task_id)
    db_session.refresh(task)
    assert task.status == "active" and task.run_count == 1

    # second run hits max_runs
    svc.advance_next_run(task.task_id)
    svc.finalize_after_run(task.task_id)
    db_session.refresh(task)
    assert task.run_count == 2
    assert task.status == "completed"


def test_finalize_never_revives_disabled(db_session):
    svc, task = _task(db_session, schedule_type="once")
    svc.advance_next_run(task.task_id)
    svc.update_task_system(task.task_id, status="disabled")

    svc.finalize_after_run(task.task_id)
    db_session.refresh(task)
    assert task.status == "disabled", "auto-disabled task must not be overridden"


def test_manual_task_never_finalized(db_session):
    svc, task = _task(db_session, schedule_type="manual")
    svc.advance_next_run(task.task_id)
    svc.finalize_after_run(task.task_id)
    db_session.refresh(task)
    assert task.status == "active", "manual tasks stay active across runs"
