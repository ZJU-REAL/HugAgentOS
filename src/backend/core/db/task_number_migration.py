"""Schema and backfill for task numbers, shared by the EE (tasknumber01) and CE (ce_0036) chains
and by the desktop SQLite startup repair (``core.db.local_schema_upgrade``).

Adds ``chat_runs.task_no`` / ``subtask_seq``, ``subagent_call_logs.task_no`` / ``child_seq`` and
the per-day ``task_number_counters`` table, then numbers existing rows by the rules new rows
follow (``core.db.models.chat_runs`` and ``core.harness.log_records``): runs in creation order by
local day; sub-agent calls under whoever dispatched them; the run's own agent keeps its number.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from core.db.models.chat_runs import subtask_number, task_number
from core.db.utc_datetime import UTCDateTime

_RUN_AGENT_TYPES = {"plan_mode", "user_agent_direct"}
_BATCH = 1000

_runs = sa.table(
    "chat_runs",
    sa.column("run_id", sa.String),
    sa.column("message_id", sa.String),
    sa.column("created_at", UTCDateTime(timezone=True)),
    sa.column("task_no", sa.String),
    sa.column("subtask_seq", sa.Integer),
)
_logs = sa.table(
    "subagent_call_logs",
    sa.column("id", sa.String),
    sa.column("message_id", sa.String),
    sa.column("subagent_type", sa.String),
    sa.column("parent_subagent_log_id", sa.String),
    sa.column("created_at", UTCDateTime(timezone=True)),
    sa.column("task_no", sa.String),
    sa.column("child_seq", sa.Integer),
)
_counters = sa.table("task_number_counters", sa.column("day", sa.String), sa.column("last_seq", sa.Integer))


_TASK_NO = re.compile(r"^RW-(\d{8})-(\d+)$")


def _local_day(at) -> str:
    if at is None:
        at = datetime.now(timezone.utc)
    elif at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return f"{at.astimezone():%Y%m%d}"


def _update(bind, table, key, rows: dict, columns: tuple[str, ...]) -> None:
    if not rows:
        return
    statement = table.update().where(table.c[key] == sa.bindparam("_key")).values(
        **{name: sa.bindparam(f"_{name}") for name in columns}
    )
    batch = [
        {"_key": row_key, **{f"_{name}": values[name] for name in columns}}
        for row_key, values in rows.items()
    ]
    for start in range(0, len(batch), _BATCH):
        bind.execute(statement, batch[start:start + _BATCH])


def _save_counters(bind, last_seq: dict[str, int]) -> None:
    if not last_seq:
        return
    insert = postgresql_insert if bind.dialect.name == "postgresql" else sqlite_insert
    statement = insert(_counters).values(day=sa.bindparam("day"), last_seq=sa.bindparam("last_seq"))
    statement = statement.on_conflict_do_update(
        index_elements=[_counters.c.day], set_={"last_seq": statement.excluded.last_seq}
    )
    bind.execute(statement, [{"day": day, "last_seq": seq} for day, seq in last_seq.items()])


def backfill_task_numbers(bind) -> dict[str, int]:
    """Number every run and sub-agent call that has no task number yet; return how many.

    Idempotent and incremental: rows that already have a number keep it, and new numbers
    continue each day's counter (and each parent's sub-task sequence) past every number
    already handed out. The Alembic revisions call it once on an empty column; the desktop
    SQLite path calls it on every start, because an older release it rolled back to keeps
    inserting runs without a number.
    """
    counters = dict(bind.execute(sa.select(_counters.c.day, _counters.c.last_seq)).all())
    runs = bind.execute(
        sa.select(_runs.c.run_id, _runs.c.message_id, _runs.c.created_at, _runs.c.task_no,
                  _runs.c.subtask_seq)
        .order_by(_runs.c.created_at, _runs.c.run_id)
    ).all()
    for row in runs:
        match = _TASK_NO.match(row.task_no or "")
        if match:
            counters[match[1]] = max(counters.get(match[1], 0), int(match[2]))

    run_no, run_by_message, run_seq = {}, {}, {}
    changed_days, run_updates = {}, {}
    for row in runs:
        if row.task_no:
            run_no[row.run_id] = row.task_no
        else:
            day = _local_day(row.created_at)
            counters[day] = counters.get(day, 0) + 1
            changed_days[day] = counters[day]
            run_no[row.run_id] = task_number(day, counters[day])
            run_updates[row.run_id] = {"task_no": run_no[row.run_id]}
        run_seq[row.run_id] = row.subtask_seq or 0
        if row.message_id is not None:
            run_by_message[row.message_id] = row.run_id

    log_updates = {}
    if sa.inspect(bind).has_table("subagent_call_logs"):
        logs = bind.execute(
            sa.select(_logs.c.id, _logs.c.message_id, _logs.c.subagent_type,
                      _logs.c.parent_subagent_log_id, _logs.c.task_no, _logs.c.child_seq)
            .where(_logs.c.message_id.isnot(None))
            .order_by(_logs.c.created_at, _logs.c.id)
        ).all()
        log_no = {row.id: row.task_no for row in logs if row.task_no}
        log_seq = {row.id: row.child_seq or 0 for row in logs}
        for row in logs:
            run_id = run_by_message.get(row.message_id)
            if row.task_no or run_id is None:
                continue
            parent = row.parent_subagent_log_id
            if row.subagent_type in _RUN_AGENT_TYPES:
                log_no[row.id] = run_no[run_id]
            elif parent in log_no:
                log_seq[parent] += 1
                log_no[row.id] = subtask_number(log_no[parent], log_seq[parent])
                log_updates.setdefault(parent, {})["child_seq"] = log_seq[parent]
            else:
                run_seq[run_id] += 1
                log_no[row.id] = subtask_number(run_no[run_id], run_seq[run_id])
                run_updates.setdefault(run_id, {})["subtask_seq"] = run_seq[run_id]
            log_updates.setdefault(row.id, {})["task_no"] = log_no[row.id]

    _save_counters(bind, changed_days)
    for table, key, updates in ((_runs, "run_id", run_updates), (_logs, "id", log_updates)):
        # One executemany per set of changed columns: a row may get a number, a new
        # sub-task sequence, or both.
        groups: dict[tuple[str, ...], dict] = {}
        for row_key, values in updates.items():
            groups.setdefault(tuple(sorted(values)), {})[row_key] = values
        for columns, rows in groups.items():
            _update(bind, table, key, rows, columns)
    return {
        "runs": sum("task_no" in v for v in run_updates.values()),
        "subagent_calls": sum("task_no" in v for v in log_updates.values()),
    }


def upgrade_task_numbers(op) -> None:
    op.create_table(
        "task_number_counters",
        sa.Column("day", sa.String(8), primary_key=True),
        sa.Column("last_seq", sa.Integer(), nullable=False),
    )
    with op.batch_alter_table("chat_runs") as batch:
        batch.add_column(sa.Column("task_no", sa.String(32), nullable=True))
        batch.add_column(sa.Column("subtask_seq", sa.Integer(), nullable=False, server_default="0"))
    with op.batch_alter_table("subagent_call_logs") as batch:
        batch.add_column(sa.Column("task_no", sa.String(64), nullable=True))
        batch.add_column(sa.Column("child_seq", sa.Integer(), nullable=False, server_default="0"))

    backfill_task_numbers(op.get_bind())

    with op.batch_alter_table("chat_runs") as batch:
        batch.alter_column("task_no", existing_type=sa.String(32), nullable=False)
        batch.create_unique_constraint("uq_chat_runs_task_no", ["task_no"])
    op.create_index("ix_subagent_call_logs_task_no", "subagent_call_logs", ["task_no"])


def downgrade_task_numbers(op) -> None:
    op.drop_index("ix_subagent_call_logs_task_no", table_name="subagent_call_logs")
    with op.batch_alter_table("subagent_call_logs") as batch:
        batch.drop_column("child_seq")
        batch.drop_column("task_no")
    with op.batch_alter_table("chat_runs") as batch:
        batch.drop_constraint("uq_chat_runs_task_no", type_="unique")
        batch.drop_column("subtask_seq")
        batch.drop_column("task_no")
    op.drop_table("task_number_counters")
