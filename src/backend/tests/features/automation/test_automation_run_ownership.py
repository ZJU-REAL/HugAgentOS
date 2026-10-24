"""An obsolete executor cannot overwrite an adopted or terminal run."""

from tests.features.automation.run_fixtures import running_run
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from features.automation.models import ScheduledTaskRun
from features.automation.runs import AutomationRunRepository
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ownership.sqlite'}")
    ScheduledTaskRun.__table__.create(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def test_takeover_rejects_old_completion_and_terminal_rewrites(factory):
    with factory() as db:
        repository = AutomationRunRepository(db)
        run_id = running_run(db, "task", "old").run_id
        assert repository.claim(run_id, "new", expected_token="old")
        assert repository.complete(run_id, lease_token="old", status="failed") is None
        completed = repository.complete(
            run_id, lease_token="new", status="success", result_summary="accepted"
        )
        assert completed.status == "success"
        assert repository.complete(run_id, lease_token="new", status="failed") is None
        assert not repository.claim(run_id, "third", expected_token="new")
        db.expire_all()
        assert db.get(ScheduledTaskRun, run_id).result_summary == "accepted"


def test_pending_row_requires_claim_before_completion(factory):
    with factory() as db:
        repository = AutomationRunRepository(db)
        run = ScheduledTaskRun(run_id="pending", task_id="task", status="pending")
        db.add(run)
        db.commit()
        run_id = run.run_id
        assert repository.complete(run_id, lease_token="new", status="success") is None
        assert repository.claim(run_id, "new", expected_token=None)
        assert repository.complete(run_id, lease_token="new", status="success") is not None


def test_conditional_result_can_rollback_with_task_bookkeeping(factory):
    with factory() as db:
        repository = AutomationRunRepository(db)
        run_id = running_run(db, "task", "owner").run_id
        assert (
            repository.complete(run_id, lease_token="owner", status="success", commit=False)
            is not None
        )
        db.rollback()
        assert db.get(ScheduledTaskRun, run_id).status == "running"


@pytest.mark.parametrize(
    "relative",
    [
        "src/backend/alembic/versions/automationlease01_run_token.py",
        "ce/overlay/src/backend/alembic/versions/ce_0031_automation_lease_token.py",
    ],
)
def test_migration_preserves_existing_rows_and_roundtrips(relative):
    root = Path(__file__).resolve().parents[5]
    spec = importlib.util.spec_from_file_location("lease_migration", root / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE scheduled_task_runs (run_id TEXT PRIMARY KEY, status TEXT)")
        )
        conn.execute(text("INSERT INTO scheduled_task_runs VALUES ('legacy', 'running')"))
        module.op = Operations(MigrationContext.configure(conn))
        module.upgrade()
        module.upgrade()  # fresh-schema create_all may already include the column
        assert "lease_token" in {
            c["name"] for c in inspect(conn).get_columns("scheduled_task_runs")
        }
        assert conn.execute(text("SELECT lease_token FROM scheduled_task_runs")).scalar() is None
        module.downgrade()
        assert "lease_token" not in {
            c["name"] for c in inspect(conn).get_columns("scheduled_task_runs")
        }
        assert conn.execute(text("SELECT status FROM scheduled_task_runs")).scalar() == "running"
        module.upgrade()
    engine.dispose()


def test_delayed_claim_cannot_replace_a_newer_claim(factory):
    with factory() as db:
        repository = AutomationRunRepository(db)
        run_id = running_run(db, "task", "original").run_id
        assert repository.claim(run_id, "current", expected_token="original")
        assert not repository.claim(run_id, "late", expected_token="original")
        assert repository.complete(run_id, lease_token="current", status="success") is not None


async def test_obsolete_observer_does_not_cancel_adopted_child(factory, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from orchestration.automations import automation_conversation as conversation

    with factory() as db:
        repository = AutomationRunRepository(db)
        run_id = running_run(db, "task", "old").run_id
    monkeypatch.setattr(conversation.db_engine, "SessionLocal", factory)
    entered = asyncio.Event()
    cancelled = []

    def get_run(_):
        entered.set()
        return SimpleNamespace(status="running")

    async def cancel_run(*args, **kwargs):
        cancelled.append(args)

    monkeypatch.setattr(conversation.runtime, "get_run", get_run)
    monkeypatch.setattr(conversation.runtime, "cancel_run", cancel_run)
    child = SimpleNamespace(run_id="child", chat_id="chat", user_id="user")
    observer = asyncio.create_task(
        conversation._wait(child, automation_run_id=run_id, lease_token="old")
    )
    await entered.wait()
    with factory() as db:
        assert AutomationRunRepository(db).claim(run_id, "new", expected_token="old")
    observer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await observer
    assert cancelled == []


async def test_expired_recovery_claimant_leaves_new_ownership_untouched(factory, monkeypatch):
    from types import SimpleNamespace

    from core.infra import ephemeral
    from features.automation.service import AutomationService
    from orchestration.automations.automation_recovery import recover_task
    from orchestration.automations.automation_scheduler import AutomationScheduler

    # Recovery reads running rows from its own session; task metadata is enough
    # because expired ownership must return before observing/starting a child.
    monkeypatch.setattr("core.db.engine.SessionLocal", factory)
    monkeypatch.setattr(
        AutomationService, "get_task_by_id", lambda *a: SimpleNamespace(user_id="u")
    )
    state = ephemeral.LocalEphemeralState()
    monkeypatch.setattr(ephemeral, "get_ephemeral_state", lambda: state)
    with factory() as db:
        run_id = running_run(db, "task", "original").run_id
    scheduler = AutomationScheduler()
    assert await scheduler._acquire_lock("task")
    lease = scheduler._leases["task"]
    await state.put(lease.key, "new", ttl=60)
    await recover_task(scheduler, "task")
    with factory() as db:
        assert db.get(ScheduledTaskRun, run_id).lease_token == "original"
    await scheduler._release_lock("task")
    assert await state.get(lease.key) == "new"


async def test_recovery_stops_before_claiming_next_run_after_lease_loss(factory, monkeypatch):
    from types import SimpleNamespace

    from core.infra import ephemeral
    from features.automation.service import AutomationService
    from orchestration.automations import automation_dispatch, automation_recovery
    from orchestration.automations.automation_scheduler import AutomationScheduler

    monkeypatch.setattr("core.db.engine.SessionLocal", factory)
    monkeypatch.setattr(
        AutomationService, "get_task_by_id", lambda *a: SimpleNamespace(user_id="u")
    )
    state = ephemeral.LocalEphemeralState()
    monkeypatch.setattr(ephemeral, "get_ephemeral_state", lambda: state)
    with factory() as db:
        repository = AutomationRunRepository(db)
        for i in range(2):
            run = running_run(db, "task", "original")
            run.chat_id = f"chat-{i}"
            db.commit()
    scheduler = AutomationScheduler()
    assert await scheduler._acquire_lock("task")
    lease = scheduler._leases["task"]
    calls = []
    adopted = []

    async def resume(*args, run_id):
        calls.append(run_id)
        await state.put(lease.key, "replacement", ttl=60)
        with factory() as db:
            other = (
                db.query(ScheduledTaskRun).filter(ScheduledTaskRun.run_id != run_id).one()
            )
            adopted.append(other.run_id)
            assert AutomationRunRepository(db).claim(
                other.run_id, "replacement", expected_token="original"
            )

    monkeypatch.setattr(automation_dispatch, "execute_task", resume)
    await automation_recovery.recover_task(scheduler, "task")
    assert len(calls) == 1
    with factory() as db:
        assert db.get(ScheduledTaskRun, adopted[0]).lease_token == "replacement"
    await scheduler._release_lock("task")


async def test_recovery_reloads_child_link_created_before_claim(factory, monkeypatch):
    from core.infra import ephemeral
    from features.automation.service import AutomationService
    from orchestration.automations import automation_dispatch, automation_recovery
    from orchestration.automations.automation_scheduler import AutomationScheduler
    from types import SimpleNamespace

    monkeypatch.setattr("core.db.engine.SessionLocal", factory)
    monkeypatch.setattr(
        AutomationService, "get_task_by_id", lambda *a: SimpleNamespace(user_id="u")
    )
    state = ephemeral.LocalEphemeralState()
    monkeypatch.setattr(ephemeral, "get_ephemeral_state", lambda: state)
    with factory() as db:
        run_id = running_run(db, "task", "original").run_id
    original_claim = AutomationRunRepository.claim

    def link_then_claim(self, run_id, token, *, expected_token):
        with factory() as old_worker:
            row = old_worker.get(ScheduledTaskRun, run_id)
            assert row.lease_token == "original"
            row.chat_id = "late-child"
            old_worker.commit()
        return original_claim(self, run_id, token, expected_token=expected_token)

    resumed = []

    async def resume(*args, run_id):
        resumed.append(run_id)

    monkeypatch.setattr(AutomationRunRepository, "claim", link_then_claim)
    monkeypatch.setattr(automation_dispatch, "execute_task", resume)
    scheduler = AutomationScheduler()
    assert await scheduler._acquire_lock("task")
    await automation_recovery.recover_task(scheduler, "task")
    assert resumed == [run_id]
    with factory() as db:
        assert db.get(ScheduledTaskRun, run_id).status == "running"
        assert db.get(ScheduledTaskRun, run_id).chat_id == "late-child"
    await scheduler._release_lock("task")
