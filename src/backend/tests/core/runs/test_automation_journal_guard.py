"""Automation cancellation and adoption share a short ownership transaction."""

import pytest

from features.automation.runs import owned_run_guard

from tests.orchestration.test_run_journal import _accept, journal_env


def test_automation_cancellation_rejects_an_obsolete_observer(journal_env):
    from features.automation.models import ScheduledTaskRun

    journal, sessions, _ = journal_env
    child = _accept(journal, "automation-child")
    with sessions() as db:
        db.add(
            ScheduledTaskRun(
                run_id="automation-run", task_id="task", status="running", lease_token="new"
            )
        )
        db.commit()
    assert not journal.cancel(
        child.run_id, reason="old observer stopped", cancel_guard=owned_run_guard("automation-run", "old")
    )
    assert journal.cancel(
        child.run_id, reason="current owner stopped", cancel_guard=owned_run_guard("automation-run", "new")
    )


async def test_terminal_worker_cleanup_rejects_false_guard(journal_env, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import core.runs.runtime as runtime

    journal, sessions, _ = journal_env
    child = _accept(journal, "terminal-cleanup")
    assert journal.cancel(child.run_id, reason="already stopped")
    monkeypatch.setattr(runtime.engine, "SessionLocal", sessions)
    worker = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setattr(
        runtime.tasks, "process_state",
        lambda: SimpleNamespace(active_runs={child.run_id: worker}),
    )
    projected = []

    async def project(*args, **kwargs):
        projected.append(args)

    monkeypatch.setattr(runtime.writer, "_write_terminal_to_stream", project)
    checked = []

    def reject(db):
        checked.append(db)
        return False

    try:
        assert not await runtime.cancel_run(
            child.run_id, user_id=child.user_id,
            stop_terminal_worker=True, cancel_guard=reject,
        )
        assert len(checked) == 1
        assert not worker.done()
        assert worker.cancelling() == 0
        assert projected == []
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize("accepted", [False, True])
async def test_terminal_projection_checks_guard(journal_env, monkeypatch, accepted):
    import core.runs.runtime as runtime

    journal, sessions, _ = journal_env
    child = _accept(journal, "terminal-projection")
    assert journal.cancel(child.run_id, reason="already stopped")
    monkeypatch.setattr(runtime.engine, "SessionLocal", sessions)
    projected = []

    async def project(*args, **kwargs):
        projected.append(args)

    monkeypatch.setattr(runtime.writer, "_write_terminal_to_stream", project)
    assert await runtime.cancel_run(
        child.run_id, user_id=child.user_id,
        stop_terminal_worker=True, cancel_guard=lambda db: accepted,
    ) is accepted
    assert len(projected) == int(accepted)
