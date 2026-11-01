"""Schema and backfill for task numbers, shared by the EE (tasknumber01) and CE (ce_0036) chains.

Adds ``chat_runs.task_no`` / ``subtask_seq``, ``subagent_call_logs.task_no`` / ``child_seq`` and
the per-day ``task_number_counters`` table, then numbers existing rows by the rules new rows
follow (``core.db.models.chat_runs`` and ``core.harness.log_records``): runs in creation order by
local day; sub-agent calls under whoever dispatched them; the run's own agent keeps its number.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timezone

import sqlalchemy as sa

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


def _local_day(at) -> str:
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return f"{at.astimezone():%Y%m%d}"


def _update(bind, table, key, values_by_key, **columns) -> None:
    statement = table.update().where(table.c[key] == sa.bindparam("_key")).values(
        **{name: sa.bindparam(f"_{name}") for name in columns}
    )
    batch = [
        {"_key": row_key, **{f"_{name}": values[row_key] for name, values in columns.items()}}
        for row_key in values_by_key
    ]
    for start in range(0, len(batch), _BATCH):
        bind.execute(statement, batch[start:start + _BATCH])


def _backfill(bind) -> None:
    run_no, run_by_message, per_day = {}, {}, Counter()
    for run_id, message_id, created_at in bind.execute(
        sa.select(_runs.c.run_id, _runs.c.message_id, _runs.c.created_at)
        .order_by(_runs.c.created_at, _runs.c.run_id)
    ):
        day = _local_day(created_at)
        per_day[day] += 1
        run_no[run_id] = f"RW-{day}-{per_day[day]:04d}"
        run_by_message[message_id] = run_id
    if per_day:
        bind.execute(_counters.insert(), [{"day": d, "last_seq": n} for d, n in per_day.items()])

    log_no, log_seq, run_seq = {}, defaultdict(int), defaultdict(int)
    for log_id, message_id, kind, parent_id in bind.execute(
        sa.select(_logs.c.id, _logs.c.message_id, _logs.c.subagent_type, _logs.c.parent_subagent_log_id)
        .where(_logs.c.message_id.isnot(None))
        .order_by(_logs.c.created_at, _logs.c.id)
    ):
        run_id = run_by_message.get(message_id)
        if run_id is None:
            continue
        if kind in _RUN_AGENT_TYPES:
            log_no[log_id] = run_no[run_id]
        elif parent_id in log_no:
            log_seq[parent_id] += 1
            log_no[log_id] = f"{log_no[parent_id]}-{log_seq[parent_id]:02d}"
        else:
            run_seq[run_id] += 1
            log_no[log_id] = f"{run_no[run_id]}-{run_seq[run_id]:02d}"

    _update(bind, _runs, "run_id", run_no, task_no=run_no, subtask_seq=defaultdict(int, run_seq))
    _update(bind, _logs, "id", log_no, task_no=log_no, child_seq=defaultdict(int, log_seq))


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

    _backfill(op.get_bind())

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
