"""Compatibility upgrades for no-Docker local SQLite databases.

The desktop service historically initialized its database with ``create_all``
instead of Alembic.  New releases therefore have to evolve an already-populated
database before the metadata reconciler can add ordinary columns and indexes.

Keep these repairs additive and rollback-compatible: an older desktop release
must still be able to write to the database while an installer is rolling back.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


def reconcile_local_chat_sequences(bind: Engine) -> dict[str, list[str]]:
    """Add and backfill durable chat sequence columns for legacy local DBs.

    ``chat_messages.chat_seq`` intentionally remains physically nullable.  The
    current ORM always allocates it, while nullable storage lets an older
    rollback target continue inserting messages.  Every newer startup repairs
    any NULL rows before serving traffic and a unique index protects assigned
    values.
    """

    report: dict[str, list[str]] = {"columns": [], "backfills": [], "indexes": []}
    with bind.begin() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        if not {"chat_sessions", "chat_messages"}.issubset(tables):
            return report

        session_columns = {
            item["name"] for item in inspector.get_columns("chat_sessions")
        }
        if "next_message_seq" not in session_columns:
            connection.exec_driver_sql(
                "ALTER TABLE chat_sessions "
                "ADD COLUMN next_message_seq BIGINT NOT NULL DEFAULT 1"
            )
            report["columns"].append("chat_sessions.next_message_seq")

        inspector = inspect(connection)
        message_columns = {
            item["name"] for item in inspector.get_columns("chat_messages")
        }
        if "chat_seq" not in message_columns:
            # See the docstring: nullable is deliberate for installer rollback
            # compatibility with releases that know nothing about chat_seq.
            connection.exec_driver_sql(
                "ALTER TABLE chat_messages ADD COLUMN chat_seq BIGINT"
            )
            report["columns"].append("chat_messages.chat_seq")

        allocated_by_chat: dict[str, int] = defaultdict(int)
        for row in connection.execute(
            text(
                "SELECT chat_id, COALESCE(MAX(chat_seq), 0) AS max_seq "
                "FROM chat_messages GROUP BY chat_id"
            )
        ).mappings():
            allocated_by_chat[str(row["chat_id"])] = int(row["max_seq"] or 0)

        # Preserve a cursor that may already be ahead of MAX(chat_seq), e.g. if
        # messages were deleted after allocation.
        for row in connection.execute(
            text("SELECT chat_id, next_message_seq FROM chat_sessions")
        ).mappings():
            chat_id = str(row["chat_id"])
            allocated_by_chat[chat_id] = max(
                allocated_by_chat[chat_id], int(row["next_message_seq"] or 1) - 1
            )

        updates: list[dict[str, Any]] = []
        for row in connection.execute(
            text(
                "SELECT message_id, chat_id FROM chat_messages "
                "WHERE chat_seq IS NULL "
                "ORDER BY chat_id, created_at, message_id"
            )
        ).mappings():
            chat_id = str(row["chat_id"])
            allocated_by_chat[chat_id] += 1
            updates.append(
                {
                    "message_id": row["message_id"],
                    "chat_seq": allocated_by_chat[chat_id],
                }
            )
        if updates:
            connection.execute(
                text(
                    "UPDATE chat_messages SET chat_seq = :chat_seq "
                    "WHERE message_id = :message_id"
                ),
                updates,
            )
            report["backfills"].append(f"chat_messages.chat_seq:{len(updates)}")

        connection.exec_driver_sql(
            "UPDATE chat_sessions "
            "SET next_message_seq = CASE "
            "WHEN next_message_seq > COALESCE(("
            "SELECT MAX(chat_messages.chat_seq) + 1 FROM chat_messages "
            "WHERE chat_messages.chat_id = chat_sessions.chat_id"
            "), 1) THEN next_message_seq "
            "ELSE COALESCE(("
            "SELECT MAX(chat_messages.chat_seq) + 1 FROM chat_messages "
            "WHERE chat_messages.chat_id = chat_sessions.chat_id"
            "), 1) END"
        )

        inspector = inspect(connection)
        sequence_key_name = "uq_chat_messages_chat_seq"
        unique_names = {
            item.get("name")
            for item in inspector.get_unique_constraints("chat_messages")
        }
        index_names = {
            item.get("name") for item in inspector.get_indexes("chat_messages")
        }
        if sequence_key_name not in unique_names | index_names:
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX uq_chat_messages_chat_seq "
                "ON chat_messages (chat_id, chat_seq)"
            )
            report["indexes"].append(sequence_key_name)

    for values in report.values():
        values.sort()
    return report


_TASK_NUMBER_COLUMNS = (
    # (table, column, DDL). ``task_no`` stays physically nullable for the same reason as
    # ``chat_messages.chat_seq``: a rollback target that predates task numbers must still
    # be able to insert runs, and every newer start numbers whatever it left behind.
    ("chat_runs", "task_no", "VARCHAR(32)"),
    ("chat_runs", "subtask_seq", "INTEGER NOT NULL DEFAULT 0"),
    ("subagent_call_logs", "task_no", "VARCHAR(64)"),
    ("subagent_call_logs", "child_seq", "INTEGER NOT NULL DEFAULT 0"),
)
_TASK_NO_UNIQUE = "uq_chat_runs_task_no"


def reconcile_local_task_numbers(bind: Engine) -> dict[str, list[str]]:
    """Add, backfill and protect task numbers on a legacy local database.

    Mirrors the CE revision ``ce_0036`` for databases that never run Alembic. It must run
    before the metadata reconciler, which refuses to add the model's non-null ``task_no``
    to a populated table. Safe on a database a failed upgrade already half-created
    (``task_number_counters`` present, columns missing) and on every later start.
    """
    from core.db.task_number_migration import backfill_task_numbers

    report: dict[str, list[str]] = {"columns": [], "backfills": [], "indexes": []}
    with bind.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        if "chat_runs" not in tables:
            return report  # fresh database: create_all builds the current schema
        for table, column, ddl in _TASK_NUMBER_COLUMNS:
            if table not in tables:
                continue
            existing = {item["name"] for item in inspect(connection).get_columns(table)}
            if column not in existing:
                connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                report["columns"].append(f"{table}.{column}")
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS task_number_counters ("
            "day VARCHAR(8) NOT NULL, last_seq INTEGER NOT NULL, PRIMARY KEY (day))"
        )

        counts = backfill_task_numbers(connection)
        for name, count in counts.items():
            if count:
                report["backfills"].append(f"task_no.{name}:{count}")

        inspector = inspect(connection)
        names = {item.get("name") for item in inspector.get_unique_constraints("chat_runs")}
        names |= {item.get("name") for item in inspector.get_indexes("chat_runs")}
        if _TASK_NO_UNIQUE not in names:
            duplicate = connection.exec_driver_sql(
                "SELECT task_no FROM chat_runs WHERE task_no IS NOT NULL "
                "GROUP BY task_no HAVING COUNT(*) > 1 LIMIT 1"
            ).first()
            if duplicate is None:
                connection.exec_driver_sql(
                    f"CREATE UNIQUE INDEX {_TASK_NO_UNIQUE} ON chat_runs (task_no)"
                )
                report["indexes"].append(_TASK_NO_UNIQUE)
            else:
                # Never block startup over the index: numbers keep working without it
                # and the next start retries once the duplicate is gone.
                logger.warning("chat_runs.task_no has duplicates (%s); unique index skipped", duplicate[0])

    for values in report.values():
        values.sort()
    return report
