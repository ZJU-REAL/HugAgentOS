"""Small, dialect-aware schema reconciliation helpers.

This module is intentionally edition-neutral.  CE uses it to recover databases
that were stamped with an early baseline revision whose physical schema varies
with the version that originally created it.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Mapping, Optional, Tuple

from sqlalchemy import Column, Computed, MetaData, inspect
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateColumn

logger = logging.getLogger(__name__)

SchemaItem = Tuple[str, str]


@contextmanager
def connection_scope(bind: Engine | Connection) -> Iterator[Connection]:
    if isinstance(bind, Connection):
        yield bind
        return
    with bind.begin() as connection:
        yield connection


def _patch_projects_kind_constraint_sqlite(connection: Connection) -> bool:
    """Relax the legacy ``kind = 'personal'`` check to allow desktop ``local``.

    SQLite bakes CHECK constraints into the table at creation and cannot ALTER
    them, so a DB created before local-mode support keeps the old constraint and
    rejects ``kind='local'`` inserts. Patch it in place via ``writable_schema``.
    Idempotent and a no-op off SQLite or once already patched (the string simply
    won't match), so it is safe to run on every startup / on every edition.
    """
    if connection.dialect.name != "sqlite":
        return False
    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='projects'"
    ).fetchone()
    if not row or not row[0]:
        return False
    old_sql = row[0]
    new_sql = old_sql.replace("CHECK (kind = 'personal')", "CHECK (kind IN ('personal', 'local'))")
    if new_sql == old_sql:
        return False
    connection.exec_driver_sql("PRAGMA writable_schema=ON")
    connection.exec_driver_sql(
        "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='projects'", (new_sql,)
    )
    # Bump the schema cookie so every (pooled) connection reloads the amended
    # table definition — without this the edit is invisible to reused connections.
    ver = connection.exec_driver_sql("PRAGMA schema_version").fetchone()[0]
    connection.exec_driver_sql(f"PRAGMA schema_version = {int(ver) + 1}")
    connection.exec_driver_sql("PRAGMA writable_schema=OFF")
    return True


def _patch_file_edit_constraints_sqlite(connection: Connection) -> list[str]:
    """Upgrade desktop checks without rewriting file bytes or credential rows.

    Desktop SQLite startup does not run Alembic. Use the same schema-cookie
    reload as the legacy project-kind repair, after missing columns are added.
    Only the two known constraint definitions may change.
    """
    import re

    if connection.dialect.name != "sqlite":
        return []
    patched = []
    for table in ("artifacts", "user_api_keys"):
        row = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not row or not row[0]:
            continue
        old_sql = row[0]
        new_sql = old_sql
        if table == "artifacts":
            new_sql = re.sub(r"CHECK\s*\(\s*size_bytes\s*>\s*0\s*\)",
                             "CHECK (size_bytes >= 0)", old_sql, flags=re.IGNORECASE)
        else:
            columns = {item["name"] for item in inspect(connection).get_columns(table)}
            if not {"agent_id", "project_id"}.issubset(columns):
                continue
            if "ck_user_api_keys_single_scope" in old_sql:
                continue
            invalid = connection.exec_driver_sql(
                "SELECT count(*) FROM user_api_keys WHERE agent_id IS NOT NULL AND project_id IS NOT NULL"
            ).scalar()
            if invalid:
                raise RuntimeError("Cannot upgrade API credentials with conflicting resource scopes")
            if not old_sql.rstrip().endswith(")"):
                raise RuntimeError("Unsupported SQLite credential table definition")
            new_sql = old_sql.rstrip()[:-1] + ", CONSTRAINT ck_user_api_keys_single_scope CHECK (agent_id IS NULL OR project_id IS NULL))"
        if new_sql == old_sql:
            continue
        connection.exec_driver_sql("PRAGMA writable_schema=ON")
        try:
            connection.exec_driver_sql(
                "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?", (new_sql, table)
            )
            version = connection.exec_driver_sql("PRAGMA schema_version").scalar()
            connection.exec_driver_sql(f"PRAGMA schema_version = {int(version) + 1}")
        finally:
            connection.exec_driver_sql("PRAGMA writable_schema=OFF")
        patched.append(table)
    return patched


def reconcile_metadata_schema(
    bind: Engine | Connection,
    metadata: MetaData,
    *,
    bootstrap_server_defaults: Optional[Mapping[SchemaItem, Any]] = None,
) -> dict[str, list[str]]:
    """Create missing tables, columns, and indexes from ``metadata``.

    ``MetaData.create_all`` only creates missing tables; it never evolves an
    existing table.  This function fills that gap without dropping or rewriting
    user data.  A non-null column without a server default is rejected because
    adding it to a populated table would be unsafe.
    """

    defaults = dict(bootstrap_server_defaults or {})
    report: dict[str, list[str]] = {
        "tables": [],
        "columns": [],
        "indexes": [],
        "constraints": [],
        "skipped_indexes": [],
    }

    with connection_scope(bind) as connection:
        before_tables = set(inspect(connection).get_table_names())
        metadata.create_all(bind=connection, checkfirst=True)
        after_tables = set(inspect(connection).get_table_names())
        report["tables"] = sorted(after_tables - before_tables)

        # Legacy-constraint self-heal (idempotent, SQLite-only, no-op elsewhere).
        if _patch_projects_kind_constraint_sqlite(connection):
            report["constraints"].append("projects.ck_projects_kind_personal")

        for table_name, table in metadata.tables.items():
            if table_name not in after_tables:
                continue

            inspector = inspect(connection)
            existing_columns = {item["name"] for item in inspector.get_columns(table_name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue

                server_default = (
                    column.server_default.arg
                    if column.server_default is not None and column.computed is None
                    else None
                )
                if server_default is None:
                    server_default = defaults.get((table_name, column.name))
                if not column.nullable and server_default is None:
                    raise RuntimeError(
                        "Cannot safely add non-null column without a server default: "
                        f"{table_name}.{column.name}"
                    )

                # SQLite cannot ALTER ADD a STORED generated column. A VIRTUAL
                # projection has the same read semantics and preserves old rows.
                generated = []
                if column.computed is not None:
                    generated.append(Computed(
                        column.computed.sqltext,
                        persisted=False if connection.dialect.name == "sqlite"
                        else column.computed.persisted,
                    ))
                migration_column = Column(
                    column.name,
                    column.type,
                    *generated,
                    nullable=column.nullable,
                    server_default=server_default,
                )
                column_sql = str(CreateColumn(migration_column).compile(dialect=connection.dialect))
                qualified_table = connection.dialect.identifier_preparer.format_table(table)
                connection.exec_driver_sql(f"ALTER TABLE {qualified_table} ADD COLUMN {column_sql}")
                report["columns"].append(f"{table_name}.{column.name}")

            existing_indexes = {
                item["name"]
                for item in inspect(connection).get_indexes(table_name)
                if item.get("name")
            }
            for index in sorted(table.indexes, key=lambda item: item.name or ""):
                if not index.name or index.name in existing_indexes:
                    continue
                # 建不上就跳过，别拖垮启动：这是一个自愈式的调和器，存量数据可能还
                # 违反某条后加的约束（历史上没有它才写进去的重复行）。跳过的索引记进
                # report["skipped_indexes"]，调用方看得见，数据修干净后下次启动补上。
                try:
                    with connection.begin_nested():
                        index.create(bind=connection, checkfirst=True)
                except SQLAlchemyError as error:
                    logger.error("Skipping index %s: %s", index.name, error)
                    report["skipped_indexes"].append(index.name)
                    continue
                report["indexes"].append(index.name)

        report["constraints"].extend(_patch_file_edit_constraints_sqlite(connection))

    for values in report.values():
        values.sort()
    return report
