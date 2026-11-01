"""Task numbers: RW-YYYYMMDD-NNNN per run, RW-…-NN per sub-agent call, backfilled on upgrade."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import core.harness.log_records as log_records
import pytest
from core.db.engine import Base
from core.db.models import ChatRun, ChatSession, SubAgentCallLog, TaskNumberCounter
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

NOON = datetime(2026, 10, 8, 4, 0, tzinfo=timezone.utc)


def _day(at: datetime) -> str:
    return f"{at.astimezone():%Y%m%d}"


@pytest.fixture()
def factory(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'task-numbers.sqlite'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        db.add(ChatSession(chat_id="chat-1", user_id="u1", title="t"))
        db.commit()
    monkeypatch.setattr(log_records, "SessionLocal", sessions)
    monkeypatch.setattr(log_records.log_writer_constants, "SUBAGENT_LOG_ENABLED", True)
    yield sessions
    engine.dispose()


def _add_run(sessions, run_id: str, at: datetime = NOON) -> str:
    with sessions() as db:
        run = ChatRun(run_id=run_id, chat_id="chat-1", user_id="u1", message_id=f"msg-{run_id}", created_at=at)
        db.add(run)
        db.commit()
        return run.task_no


def _sub(sessions, log_id: str, run_id: str, *, kind: str = "user_agent", parent: str | None = None) -> str:
    log_records.write_subagent_create_sync({
        "id": log_id, "run_id": run_id, "subagent_name": log_id, "subagent_type": kind,
        "parent_subagent_log_id": parent, "message_id": f"msg-{run_id}",
    })
    with sessions() as db:
        return db.get(SubAgentCallLog, log_id).task_no


def test_runs_are_numbered_per_day_in_order(factory):
    next_day = NOON + timedelta(days=1)
    numbers = [_add_run(factory, "r1"), _add_run(factory, "r2"), _add_run(factory, "r3", next_day)]
    assert numbers == [f"RW-{_day(NOON)}-0001", f"RW-{_day(NOON)}-0002", f"RW-{_day(next_day)}-0001"]


def test_concurrent_admissions_never_share_a_number(factory):
    with ThreadPoolExecutor(max_workers=8) as pool:
        numbers = list(pool.map(lambda i: _add_run(factory, f"c{i}"), range(24)))
    assert sorted(numbers) == [f"RW-{_day(NOON)}-{n:04d}" for n in range(1, 25)]
    with factory() as db:
        assert db.get(TaskNumberCounter, _day(NOON)).last_seq == 24


def test_subtasks_are_numbered_under_whoever_dispatched_them(factory):
    main = _add_run(factory, "r1")
    assert _sub(factory, "a", "r1") == f"{main}-01"
    assert _sub(factory, "b", "r1") == f"{main}-02"
    assert _sub(factory, "a1", "r1", parent="a") == f"{main}-01-01"  # called by sub-agent a
    assert _sub(factory, "c", "r1") == f"{main}-03"


def test_plan_steps_number_under_the_run_and_the_coordinator_keeps_its_number(factory):
    main = _add_run(factory, "r1")
    assert _sub(factory, "plan", "r1", kind="plan_mode") == main
    assert _sub(factory, "s1", "r1", kind="plan_step", parent="plan") == f"{main}-01"
    assert _sub(factory, "s2", "r1", kind="plan_step", parent="plan") == f"{main}-02"


def test_subagent_outside_any_run_has_no_number(factory):
    assert _sub(factory, "x", "") is None


# ── Upgrade backfill ───────────────────────────────────────────────────────


def _alembic(repo_root: Path, url: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=repo_root,
        env={**os.environ, "DATABASE_URL": url}, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_upgrade_numbers_existing_runs_and_subagent_calls(tmp_path):
    repo_root = Path(__file__).resolve().parents[5]
    revision, previous = (
        ("ce_0036", "ce_0035")
        if (repo_root / ".hugagent-edition").is_file()
        else ("tasknumber01", "taskmonitor02")
    )
    url = f"sqlite:///{tmp_path / 'backfill.sqlite'}"
    engine = create_engine(url)
    # The full chain does not run from scratch on SQLite; start from the current schema and
    # step back over this revision only.
    Base.metadata.create_all(engine)
    _alembic(repo_root, url, "stamp", revision)
    _alembic(repo_root, url, "downgrade", previous)
    day1, day2 = NOON, NOON + timedelta(days=1)
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO chat_sessions (chat_id, user_id, title, created_at, updated_at) "
            "VALUES ('c', 'u', 't', :at, :at)"
        ), {"at": day1})
        for run_id, at in (("late", day2), ("first", day1), ("second", day1 + timedelta(minutes=1))):
            db.execute(text(
                "INSERT INTO chat_runs (run_id, chat_id, user_id, message_id, status, last_event_offset, "
                "run_phase, operation_seq, snapshot_version, last_operation_safety, created_at, updated_at) "
                "VALUES (:r, 'c', 'u', :m, 'completed', 0, 'completed', 0, 0, 'replayable', :at, :at)"
            ), {"r": run_id, "m": f"m-{run_id}", "at": at})
        for i, (log_id, kind, parent) in enumerate((
            ("plan", "plan_mode", None), ("s1", "plan_step", "plan"), ("s2", "plan_step", "plan"),
            ("helper", "user_agent", "s1"),
        )):
            db.execute(text(
                "INSERT INTO subagent_call_logs (id, message_id, subagent_name, subagent_type, "
                "parent_subagent_log_id, status, created_at) VALUES (:i, 'm-first', :i, :k, :p, 'success', :at)"
            ), {"i": log_id, "k": kind, "p": parent, "at": day1 + timedelta(seconds=i)})
    _alembic(repo_root, url, "upgrade", revision)

    with engine.connect() as db:
        runs = dict(db.execute(text("SELECT run_id, task_no FROM chat_runs")).all())
        logs = dict(db.execute(text("SELECT id, task_no FROM subagent_call_logs")).all())
        counters = dict(db.execute(text("SELECT day, last_seq FROM task_number_counters")).all())
    first = f"RW-{_day(day1)}-0001"
    assert runs == {"first": first, "second": f"RW-{_day(day1)}-0002", "late": f"RW-{_day(day2)}-0001"}
    assert logs == {"plan": first, "s1": f"{first}-01", "s2": f"{first}-02", "helper": f"{first}-01-01"}
    assert counters == {_day(day1): 2, _day(day2): 1}

    with sessionmaker(bind=engine)() as db:  # new runs continue the day's sequence
        db.add(ChatRun(run_id="new", chat_id="c", user_id="u", message_id="m-new", created_at=day1))
        db.commit()
        assert db.get(ChatRun, "new").task_no == f"RW-{_day(day1)}-0003"
    _alembic(repo_root, url, "downgrade", previous)
    engine.dispose()
