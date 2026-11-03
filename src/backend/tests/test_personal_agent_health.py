"""HealthBoard rows for background loops, per-run telemetry and the read-only watch view."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from personal_agent import health, telemetry
from personal_agent.background import BackgroundServices
from personal_agent.health import HealthEntry, PersonalRuntimeHealth, board
from personal_agent.telemetry import PersonalAssignmentTelemetry
from tests.test_personal_agent_monitor import sessions  # noqa: F401


@pytest.fixture
def db_factory(sessions, monkeypatch):
    monkeypatch.setattr("core.db.engine.SessionLocal", sessions)
    monkeypatch.setattr(health, "MIN_BEAT_SEC", 0.01)
    return sessions


def entry(db_factory, name):
    with db_factory() as db:
        return next(e for e in board(db) if e["name"] == name)


async def until(check, timeout=5):
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


async def test_failing_loop_turns_error_after_three_cycles_and_recovers(db_factory):
    calls, failing = [], [True]
    statuses = []

    async def cycle():
        if calls:
            statuses.append(entry(db_factory, "loop.probe")["status"])
        calls.append(1)
        if failing[0]:
            raise RuntimeError("boom")

    services = BackgroundServices("owner-a")
    task = asyncio.create_task(services.every(0.01, "probe", cycle))
    try:
        await until(lambda: len(calls) >= 4)
        # Seen at the start of cycles 2, 3 and 4: two failures stay ok, the third is an error.
        assert statuses[:3] == ["ok", "ok", "error"]
        row = entry(db_factory, "loop.probe")
        assert row["failures"] >= 3 and row["failing_since"] and "RuntimeError: boom" in row["detail"]
        failing[0] = False
        await until(lambda: entry(db_factory, "loop.probe")["status"] == "ok")
        row = entry(db_factory, "loop.probe")
        assert row["failures"] == 0 and row["failing_since"] is None and not row["stale"]
    finally:
        services.stopping = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stopped_loop_reads_as_error_within_three_intervals(db_factory, monkeypatch):
    services = BackgroundServices("owner-a")
    task = asyncio.create_task(services.every(10, "quiet", lambda: asyncio.sleep(0)))
    await until(lambda: any(e["name"] == "loop.quiet" for e in board_rows(db_factory)))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert entry(db_factory, "loop.quiet")["status"] == "ok"
    later = health.utc_now() + timedelta(seconds=31)
    monkeypatch.setattr(health, "utc_now", lambda: later)
    row = entry(db_factory, "loop.quiet")
    assert row["status"] == "error" and row["stale"] and row["detail"] == "no heartbeat"


def board_rows(db_factory):
    with db_factory() as db:
        return board(db)


async def test_watched_loop_beats_and_is_marked_down_when_it_exits(db_factory, monkeypatch):
    import personal_agent.background as background

    monkeypatch.setattr(background, "TURN_BEAT_SEC", 0.02)
    release = asyncio.Event()

    async def turn_loop():
        await release.wait()
        raise ValueError("lost")

    services = BackgroundServices("owner-a")
    task = asyncio.create_task(services.watch("turn", turn_loop()))
    await until(lambda: any(e["name"] == "loop.turn" for e in board_rows(db_factory)))
    first = entry(db_factory, "loop.turn")["beat_at"]
    await until(lambda: entry(db_factory, "loop.turn")["beat_at"] != first)
    assert entry(db_factory, "loop.turn")["status"] == "ok"
    release.set()
    await task
    row = entry(db_factory, "loop.turn")
    assert row["status"] == "error" and row["detail"] == "ValueError: lost"


def test_rows_have_one_writer_and_dead_owners_are_replaced(db_factory, monkeypatch):
    mine, theirs = HealthEntry("loop.x", "a", 1), HealthEntry("loop.x", "b", 1)
    mine.register(), theirs.register()
    for _ in range(3):
        mine.error("down")
    statuses = {e["owner"]: e["status"] for e in board_rows(db_factory) if e["name"] == "loop.x"}
    assert statuses == {"a": "error", "b": "ok"}
    later = health.utc_now() + timedelta(seconds=10)
    monkeypatch.setattr(health, "utc_now", lambda: later)
    HealthEntry("loop.x", "c", 1).register()
    assert {e["owner"] for e in board_rows(db_factory) if e["name"] == "loop.x"} == {"c"}


def test_worker_cycle_failures_reach_the_board(db_factory, monkeypatch):
    from personal_agent.worker import executor as worker_module

    worker = worker_module.Executor()
    calls = []

    def claim():
        calls.append(1)
        if len(calls) >= 4:
            worker.stopping = True
        raise RuntimeError("database down")

    async def resume():
        pass

    monkeypatch.setattr(worker, "claim", claim)
    monkeypatch.setattr(worker, "recover", lambda: None)
    monkeypatch.setattr(worker_module.computer_wait, "resume_available", resume)
    monkeypatch.setattr(worker_module, "CLAIM_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(worker_module, "SessionLocal", db_factory)
    asyncio.run(worker.loop())
    row = entry(db_factory, "worker")
    assert row["owner"] == worker.identity and row["status"] == "error" and row["failures"] == 4


def test_observe_counts_steps_tools_tokens_and_repeats():
    state = telemetry.RunTelemetry("task", "run")
    assert telemetry.observe(state, "tool_call", {"name": "pa_bash", "args": {"cmd": "ls"}})
    progress = state.last_progress_at
    assert progress is not None
    telemetry.observe(state, "tool_call", {"name": "pa_bash", "args": {"cmd": "ls"}})
    assert state.last_progress_at == progress  # A repeated call is no progress.
    telemetry.observe(state, "context_usage", {"prompt_tokens": 100, "completion_tokens": 7})
    assert not telemetry.observe(state, "text_delta", "x")
    for i in range(25):
        telemetry.observe(state, "tool_call", {"name": "pa_read", "args": {"path": str(i)}})
    assert state.steps == 27 and state.tool_counts == {"pa_bash": 2, "pa_read": 25}
    assert len(state.recent_steps) == telemetry.RECENT_STEPS and state.recent_steps[-1][0] == "pa_read"
    assert (state.prompt_tokens, state.completion_tokens) == (100, 7)


async def test_task_run_records_telemetry_and_watch_view(db_factory, monkeypatch):
    import time
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.deps import require_admin
    from core.harness.hooks import HookStage, Invocation
    from personal_agent.api import watch
    from personal_agent.context import AgentContext
    from personal_agent.models import PersonalWorkItem

    with db_factory() as db:
        ident = AgentContext(db, "owner").tasks.submit("抓取热搜", "initial")["id"]
        task = db.get(PersonalWorkItem, ident)
        task.status, task.owner, task.run_id, task.lease_until = "running", "worker", "pa_asg_1", time.time() + 100
        db.commit()
    tool, model, finish = telemetry.hooks(ident, "pa_asg_1")
    for args in ({"q": "news"}, {"q": "news"}, {"path": "a.md"}):
        name = "pa_search" if "q" in args else "pa_write"
        await tool.handler(Invocation.create(run_id="run", stage=HookStage.BEFORE_TOOL, operation_name=name,
                                             data={"tool_args": args, "raw_input": args}))
        await model.handler(Invocation.create(run_id="run", stage=HookStage.AFTER_MODEL, operation_name="model",
                                              data={"response": {"usage": {"input_tokens": 50, "output_tokens": 5}}}))
    await finish.handler(Invocation.create(run_id="run", stage=HookStage.BEFORE_FINISH, operation_name="agent_reply"))
    with db_factory() as db:
        row = db.query(PersonalAssignmentTelemetry).one()
        assert (row.work_item_id, row.run_id, row.steps) == (ident, "pa_asg_1", 3)
        assert row.tool_counts == {"pa_search": 2, "pa_write": 1}
        assert [name for name, _ in row.recent_steps] == ["pa_search", "pa_search", "pa_write"]
        assert (row.prompt_tokens, row.completion_tokens) == (150, 15)
        assert row.last_progress_at is not None

    monkeypatch.setattr(health, "MIN_BEAT_SEC", 0.01)
    failing = HealthEntry("loop.channel", "w", 3)
    await asyncio.to_thread(failing.register)
    await asyncio.to_thread(failing.error, "TimeoutError: im")
    app = FastAPI()
    app.include_router(watch.router, prefix="/internal/personal/watch")
    client = TestClient(app)
    assert client.get("/internal/personal/watch/health").status_code in (401, 503)
    app.dependency_overrides[require_admin] = lambda: None
    data = client.get("/internal/personal/watch/health").json()["data"]
    assert [e["name"] for e in data["health"]] == ["loop.channel"]
    assert data["failures"][0]["detail"] == "TimeoutError: im" and data["failures"][0]["count"] == 1
    [summary] = data["telemetry"]
    assert summary["work_item_id"] == ident and summary["steps"] == 3 and summary["distinct_recent_steps"] == 2


def test_watch_route_is_mounted_under_internal_prefix():
    from personal_agent.api.agent import router

    paths = {route.path for route in router.routes}
    assert "/internal/personal/watch/health" in paths


def test_migrations_create_and_drop_both_tables():
    import importlib.util
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    for filename in ("src/backend/alembic/versions/pahealth01_runtime_health_telemetry.py",
                     "ce/overlay/src/backend/alembic/versions/ce_0038_personal_health.py"):
        spec = importlib.util.spec_from_file_location("health_migration", filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        engine = create_engine("sqlite://")
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.upgrade()  # Idempotent where CE's create_all already made the tables.
            tables = set(inspect(conn).get_table_names())
            assert {"personal_runtime_health", "personal_assignment_telemetry"} <= tables
            columns = {c["name"] for c in inspect(conn).get_columns("personal_runtime_health")}
            assert {c.name for c in PersonalRuntimeHealth.__table__.columns} == columns
            columns = {c["name"] for c in inspect(conn).get_columns("personal_assignment_telemetry")}
            assert {c.name for c in PersonalAssignmentTelemetry.__table__.columns} == columns
            module.downgrade()
            assert not {"personal_runtime_health", "personal_assignment_telemetry"} & set(inspect(conn).get_table_names())
