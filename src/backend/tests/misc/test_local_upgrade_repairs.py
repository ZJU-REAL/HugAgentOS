"""Desktop SQLite upgrades that never run Alembic: task numbers, leftover columns, preflight."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import core.db.models  # noqa: F401  registers every table
from core.db.engine import Base
from core.db.local_schema_upgrade import reconcile_local_task_numbers
from core.db.orphan_columns import drop_not_null_sql
from core.db.schema_reconcile import reconcile_metadata_schema

NOON = datetime(2026, 10, 8, 4, 0, tzinfo=timezone.utc)
_REMOVED = {
    "chat_runs": {"task_no", "subtask_seq"},
    "subagent_call_logs": {"task_no", "child_seq"},
}


def _day(at: datetime) -> str:
    return f"{at.astimezone():%Y%m%d}"


def _legacy_metadata() -> MetaData:
    """Current metadata as an install from before task numbers had it.

    No task-number columns or counters table, and ``admin_mcp_servers`` still carries the
    NOT NULL ``is_stable`` flag a later release stopped writing.
    """
    legacy = MetaData()
    for name, table in Base.metadata.tables.items():
        if name == "task_number_counters":
            continue
        if name in _REMOVED or name == "admin_mcp_servers":
            columns = [column._copy() for column in table.columns if column.name not in _REMOVED.get(name, ())]
            if name == "admin_mcp_servers":
                columns.append(Column("is_stable", Boolean, nullable=False))
            Table(name, legacy, *columns)
        else:
            table.to_metadata(legacy)
    return legacy


@pytest.fixture()
def legacy_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite'}")
    _legacy_metadata().create_all(engine)
    with engine.begin() as db:
        db.execute(text(
            "INSERT INTO chat_sessions (chat_id, user_id, title, created_at, updated_at) "
            "VALUES ('c', 'u', 't', :at, :at)"
        ), {"at": NOON})
        for run_id, at in (("late", NOON + timedelta(days=1)), ("first", NOON),
                           ("second", NOON + timedelta(minutes=1))):
            _insert_run(db, run_id, at)
        for i, (log_id, kind, parent) in enumerate((
            ("plan", "plan_mode", None), ("s1", "plan_step", "plan"), ("helper", "user_agent", "s1"),
        )):
            _insert_log(db, log_id, "m-first", kind, parent, NOON + timedelta(seconds=i))
    yield engine
    engine.dispose()


def _insert_run(db, run_id: str, at: datetime) -> None:
    db.execute(text(
        "INSERT INTO chat_runs (run_id, chat_id, user_id, message_id, status, last_event_offset, "
        "run_phase, operation_seq, snapshot_version, last_operation_safety, created_at, updated_at) "
        "VALUES (:r, 'c', 'u', :m, 'completed', 0, 'completed', 0, 0, 'replayable', :at, :at)"
    ), {"r": run_id, "m": f"m-{run_id}", "at": at})


def _insert_log(db, log_id: str, message_id: str, kind: str, parent: str | None, at: datetime) -> None:
    db.execute(text(
        "INSERT INTO subagent_call_logs (id, message_id, subagent_name, subagent_type, "
        "parent_subagent_log_id, status, created_at) VALUES (:i, :m, :i, :k, :p, 'success', :at)"
    ), {"i": log_id, "m": message_id, "k": kind, "p": parent, "at": at})


def _numbers(engine):
    with engine.connect() as db:
        return (
            dict(db.execute(text("SELECT run_id, task_no FROM chat_runs")).all()),
            dict(db.execute(text("SELECT id, task_no FROM subagent_call_logs")).all()),
            dict(db.execute(text("SELECT day, last_seq FROM task_number_counters")).all()),
        )


def test_the_unrepaired_upgrade_is_refused_before_anything_changes(legacy_engine):
    tables_before = set(inspect(legacy_engine).get_table_names())

    with pytest.raises(RuntimeError, match="chat_runs.task_no"):
        reconcile_metadata_schema(legacy_engine, Base.metadata)

    # SQLite DDL is not transactional: the refusal has to come before create_all.
    assert set(inspect(legacy_engine).get_table_names()) == tables_before


def test_legacy_runs_are_numbered_like_the_alembic_revision(legacy_engine):
    first_report = reconcile_local_task_numbers(legacy_engine)
    runs, logs, counters = _numbers(legacy_engine)

    d1, d2 = _day(NOON), _day(NOON + timedelta(days=1))
    first = f"RW-{d1}-0001"
    assert runs == {"first": first, "second": f"RW-{d1}-0002", "late": f"RW-{d2}-0001"}
    assert logs == {"plan": first, "s1": f"{first}-01", "helper": f"{first}-01-01"}
    assert counters == {d1: 2, d2: 1}
    assert "uq_chat_runs_task_no" in first_report["indexes"]
    assert reconcile_local_task_numbers(legacy_engine) == {"columns": [], "backfills": [], "indexes": []}


def test_startup_survives_a_database_a_failed_upgrade_half_created(legacy_engine):
    # What 0.5.160 left behind: create_all ran, then the column add was refused.
    with legacy_engine.begin() as db:
        db.exec_driver_sql(
            "CREATE TABLE task_number_counters (day VARCHAR(8) NOT NULL, last_seq INTEGER NOT NULL, "
            "PRIMARY KEY (day))"
        )

    reconcile_local_task_numbers(legacy_engine)
    report = reconcile_metadata_schema(legacy_engine, Base.metadata)

    assert report["relaxed_columns"] == ["admin_mcp_servers.is_stable"]
    assert _numbers(legacy_engine)[0]["first"] == f"RW-{_day(NOON)}-0001"


def test_rows_a_rolled_back_release_wrote_are_numbered_after_existing_ones(legacy_engine):
    reconcile_local_task_numbers(legacy_engine)
    reconcile_metadata_schema(legacy_engine, Base.metadata)
    sessions = sessionmaker(bind=legacy_engine)
    from core.db.models import ChatRun

    with sessions() as db:  # current release admits one more run that day
        db.add(ChatRun(run_id="current", chat_id="c", user_id="u", message_id="m-current", created_at=NOON))
        db.commit()
    with legacy_engine.begin() as db:  # the rollback target knows nothing about numbers
        _insert_run(db, "rolled-back", NOON + timedelta(hours=1))
        _insert_log(db, "late-helper", "m-first", "user_agent", "plan", NOON + timedelta(hours=1))
        _insert_log(db, "late-direct", "m-second", "user_agent", None, NOON + timedelta(hours=2))

    report = reconcile_local_task_numbers(legacy_engine)
    runs, logs, counters = _numbers(legacy_engine)

    d1 = _day(NOON)
    assert runs["current"] == f"RW-{d1}-0003"
    assert runs["rolled-back"] == f"RW-{d1}-0004"
    assert counters[d1] == 4
    # The plan's own agent already had one child (s1), so its next one is -02.
    assert logs["late-helper"] == f"RW-{d1}-0001-02"
    assert logs["late-direct"] == f"RW-{d1}-0002-01"
    assert report["backfills"] == ["task_no.runs:1", "task_no.subagent_calls:2"]
    with sessions() as db:  # and the live counter carries on past the backfill
        db.add(ChatRun(run_id="next", chat_id="c", user_id="u", message_id="m-next", created_at=NOON))
        db.commit()
        assert db.get(ChatRun, "next").task_no == f"RW-{d1}-0005"


def test_leftover_not_null_column_no_longer_blocks_inserts(legacy_engine):
    reconcile_local_task_numbers(legacy_engine)
    reconcile_metadata_schema(legacy_engine, Base.metadata)
    from core.db.models import AdminMcpServer

    with sessionmaker(bind=legacy_engine)() as db:
        db.add(AdminMcpServer(server_id="probe", display_name="Probe", description="", transport="stdio",
                              command="true", is_enabled=True, sort_order=0))
        db.commit()
    with legacy_engine.connect() as db:
        assert db.execute(text("SELECT is_stable FROM admin_mcp_servers WHERE server_id = 'probe'")).scalar() is None
    # Idempotent: nothing left to relax on the next start.
    assert reconcile_metadata_schema(legacy_engine, Base.metadata)["relaxed_columns"] == []


@pytest.mark.parametrize(
    ("sql", "column", "expected"),
    [
        ('CREATE TABLE t (\n\tid INTEGER NOT NULL, \n\tflag BOOLEAN NOT NULL, \n\tPRIMARY KEY (id)\n)',
         "flag", 'CREATE TABLE t (\n\tid INTEGER NOT NULL, \n\tflag BOOLEAN, \n\tPRIMARY KEY (id)\n)'),
        ('CREATE TABLE t (id INTEGER, "Flag" BOOLEAN NOT NULL)', "flag",
         'CREATE TABLE t (id INTEGER, "Flag" BOOLEAN)'),
        ("CREATE TABLE t (n NUMERIC(10, 2) NOT NULL, flag INT)", "n", "CREATE TABLE t (n NUMERIC(10, 2), flag INT)"),
        # Ambiguous definitions are left alone rather than guessed at.
        ("CREATE TABLE t (flag INT NOT NULL CHECK (flag IS NOT NULL))", "flag", None),
        ("CREATE TABLE t (flag INT NOT NULL ON CONFLICT REPLACE)", "flag", None),
        ("CREATE TABLE t (other INT NOT NULL, CONSTRAINT flag CHECK (other > 0))", "flag", None),
    ],
)
def test_not_null_is_removed_only_from_the_named_column(sql, column, expected):
    assert drop_not_null_sql(sql, column) == expected


def test_preflight_names_every_unsafe_column(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'preflight.sqlite'}")
    old = MetaData()
    Table("things", old, Column("id", Integer, primary_key=True))
    old.create_all(engine)
    new = MetaData()
    Table("things", new, Column("id", Integer, primary_key=True),
          Column("b", String(8), nullable=False), Column("a", String(8), nullable=False),
          Column("ok", String(8), nullable=False, server_default="x"))
    Table("brand_new", new, Column("id", Integer, primary_key=True))

    with pytest.raises(RuntimeError, match=r"things\.a, things\.b\."):
        reconcile_metadata_schema(engine, new)
    assert "brand_new" not in inspect(engine).get_table_names()


def test_old_inbox_rows_are_requeued_and_classified_whatever_their_status(tmp_path):
    from core.db.local_personal_inbox_upgrade import reconcile_local_personal_inbox

    engine = create_engine(f"sqlite:///{tmp_path / 'inbox.sqlite'}")
    with engine.begin() as db:
        db.exec_driver_sql("CREATE TABLE chat_messages (message_id VARCHAR(64) PRIMARY KEY, chat_id VARCHAR(64))")
        db.exec_driver_sql(
            "CREATE TABLE personal_message_inbox (message_id VARCHAR(64) PRIMARY KEY, payload JSON, "
            "status VARCHAR(24), kind VARCHAR(24) NOT NULL DEFAULT 'message', chat_id VARCHAR(64), "
            "turn_id VARCHAR(64))"
        )
        rows = [  # message_id, payload, status, turn_id
            ("hb-pending", '{"kind": "heartbeat"}', "responding", None),
            ("task-failed", '{"kind": "task_event", "event": "completed"}', "failed", None),
            ("run-done", '{"kind": "task_event", "event": "session_run_finished"}', "done", None),
            ("old-claim", '{"content": "hi"}', "claimed", None),
            ("live-claim", '{"content": "hi"}', "claimed", "turn-1"),
        ]
        for message_id, payload, status, turn_id in rows:
            db.execute(text("INSERT INTO chat_messages VALUES (:m, 'chat-1')"), {"m": message_id})
            db.execute(text(
                "INSERT INTO personal_message_inbox (message_id, payload, status, turn_id) "
                "VALUES (:m, :p, :s, :t)"
            ), {"m": message_id, "p": payload, "s": status, "t": turn_id})

    report = reconcile_local_personal_inbox(engine)

    with engine.connect() as db:
        state = {m: (k, s, c) for m, k, s, c in db.execute(text(
            "SELECT message_id, kind, status, chat_id FROM personal_message_inbox"))}
    assert state == {
        "hb-pending": ("heartbeat", "pending", "chat-1"),
        "task-failed": ("task_result", "failed", "chat-1"),
        "run-done": ("session_run", "done", "chat-1"),
        "old-claim": ("message", "pending", "chat-1"),
        "live-claim": ("message", "claimed", "chat-1"),  # the current router's own claim
    }
    assert report == {"requeued": ["personal_message_inbox:2"], "kinds": ["personal_message_inbox:3"]}
    assert reconcile_local_personal_inbox(engine) == {"requeued": [], "kinds": []}
