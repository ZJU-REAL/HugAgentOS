"""Small, dialect-aware schema reconciliation helpers.

This module is intentionally edition-neutral.  CE uses it to recover databases
that were stamped with an early baseline revision whose physical schema varies
with the version that originally created it.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Mapping, Optional, Tuple

from sqlalchemy import Column, MetaData, inspect
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.schema import CreateColumn

SchemaItem = Tuple[str, str]


@contextmanager
def _connection(bind: Engine | Connection) -> Iterator[Connection]:
    if isinstance(bind, Connection):
        yield bind
        return
    with bind.begin() as connection:
        yield connection


def reconcile_metadata_schema(
    bind: Engine | Connection,
    metadata: MetaData,
    *,
    bootstrap_server_defaults: Optional[Mapping[SchemaItem, str]] = None,
) -> dict[str, list[str]]:
    """Create missing tables, columns, and indexes from ``metadata``.

    ``MetaData.create_all`` only creates missing tables; it never evolves an
    existing table.  This function fills that gap without dropping or rewriting
    user data.  A non-null column without a server default is rejected because
    adding it to a populated table would be unsafe.
    """

    defaults = dict(bootstrap_server_defaults or {})
    report: dict[str, list[str]] = {"tables": [], "columns": [], "indexes": []}

    with _connection(bind) as connection:
        before_tables = set(inspect(connection).get_table_names())
        metadata.create_all(bind=connection, checkfirst=True)
        after_tables = set(inspect(connection).get_table_names())
        report["tables"] = sorted(after_tables - before_tables)

        for table_name, table in metadata.tables.items():
            if table_name not in after_tables:
                continue

            inspector = inspect(connection)
            existing_columns = {item["name"] for item in inspector.get_columns(table_name)}
            for column in table.columns:
                if column.name in existing_columns:
                    continue

                server_default = (
                    column.server_default.arg if column.server_default is not None else None
                )
                if server_default is None:
                    server_default = defaults.get((table_name, column.name))
                if not column.nullable and server_default is None:
                    raise RuntimeError(
                        "Cannot safely add non-null column without a server default: "
                        f"{table_name}.{column.name}"
                    )

                migration_column = Column(
                    column.name,
                    column.type,
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
                index.create(bind=connection, checkfirst=True)
                report["indexes"].append(index.name)

    for values in report.values():
        values.sort()
    return report
