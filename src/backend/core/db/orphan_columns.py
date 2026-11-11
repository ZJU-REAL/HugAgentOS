"""Relax NOT NULL on columns the ORM no longer writes.

A model column that is removed is dropped by an Alembic revision, but CE databases evolved
by the metadata reconciler (every desktop SQLite database, and CE servers whose chain has
no matching drop) keep it. When that leftover column is NOT NULL without a default, every
ORM insert into the table fails, e.g. ``admin_mcp_servers.is_stable`` after the native MCP
runtime stopped writing it: no MCP server could be installed or synced any more.

Dropping the column would break the release an installer rolls back to if that release
still reads it. Relaxing NOT NULL is enough for inserts and keeps every release working:
existing values stay, a release that knows the column still writes it.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import MetaData, inspect
from sqlalchemy.engine import Connection

logger = logging.getLogger(__name__)

_TABLE_CONSTRAINT = re.compile(r"^(CONSTRAINT|PRIMARY|UNIQUE|CHECK|FOREIGN)\b", re.IGNORECASE)
_NOT_NULL = re.compile(r"\s+NOT\s+NULL\b", re.IGNORECASE)


def _split_definitions(body: str) -> list[str]:
    """Split a CREATE TABLE body on top-level commas (outside parentheses and quotes)."""
    parts, depth, quote, start = [], 0, "", 0
    for index, char in enumerate(body):
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'`[":
            quote = "]" if char == "[" else char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(body[start:index])
            start = index + 1
    parts.append(body[start:])
    return parts


def _column_name(definition: str) -> str:
    text = definition.strip()
    if text[:1] in "\"`[":
        closing = "]" if text[0] == "[" else text[0]
        end = text.find(closing, 1)
        return text[1:end] if end > 0 else ""
    return text.split(None, 1)[0] if text else ""


def drop_not_null_sql(create_sql: str, column: str) -> str | None:
    """``create_sql`` with NOT NULL removed from ``column``'s definition, or None.

    None whenever the edit is not unambiguous: the column is not found, its definition
    holds NOT NULL more than once (e.g. inside a CHECK) or an ON CONFLICT clause.
    """
    open_at, close_at = create_sql.find("("), create_sql.rfind(")")
    if open_at < 0 or close_at <= open_at:
        return None
    parts = _split_definitions(create_sql[open_at + 1:close_at])
    for index, part in enumerate(parts):
        if _TABLE_CONSTRAINT.match(part.strip()) or _column_name(part).lower() != column.lower():
            continue
        if re.search(r"\bON\s+CONFLICT\b", part, re.IGNORECASE):
            return None
        relaxed, count = _NOT_NULL.subn("", part)
        if count != 1:
            return None
        parts[index] = relaxed
        return create_sql[:open_at + 1] + ",".join(parts) + create_sql[close_at:]
    return None


def _relax_sqlite(connection: Connection, table: str, column: str) -> bool:
    row = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    new_sql = drop_not_null_sql(row[0], column) if row and row[0] else None
    if new_sql is None:
        return False
    # Same in-place edit as the other SQLite constraint repairs in schema_reconcile:
    # NOT NULL is only checked on write, so the stored rows are untouched.
    connection.exec_driver_sql("PRAGMA writable_schema=ON")
    try:
        connection.exec_driver_sql(
            "UPDATE sqlite_master SET sql=? WHERE type='table' AND name=?", (new_sql, table)
        )
        version = connection.exec_driver_sql("PRAGMA schema_version").scalar()
        connection.exec_driver_sql(f"PRAGMA schema_version = {int(version) + 1}")
    finally:
        connection.exec_driver_sql("PRAGMA writable_schema=OFF")
    still_required = any(
        item[1] == column and item[3]
        for item in connection.exec_driver_sql(f'PRAGMA table_info("{table}")')
    )
    if still_required:
        raise RuntimeError(f"Could not relax NOT NULL on {table}.{column}")
    return True


def relax_orphan_not_null_columns(connection: Connection, metadata: MetaData) -> list[str]:
    """Make leftover NOT NULL columns without a default nullable; return ``table.column``s."""
    dialect = connection.dialect.name
    if dialect not in ("sqlite", "postgresql"):
        return []
    preparer = connection.dialect.identifier_preparer
    present = set(inspect(connection).get_table_names())
    relaxed = []
    for name, table in sorted(metadata.tables.items()):
        if name not in present:
            continue
        inspector = inspect(connection)
        modelled = {column.name for column in table.columns}
        primary = set(inspector.get_pk_constraint(name).get("constrained_columns") or [])
        for item in inspector.get_columns(name):
            column = item["name"]
            if (column in modelled or column in primary or item["nullable"]
                    or item.get("default") is not None or item.get("computed")
                    or item.get("identity")):
                continue
            if dialect == "sqlite":
                if not _relax_sqlite(connection, name, column):
                    logger.warning("Leftover NOT NULL column %s.%s left as is: "
                                   "its definition could not be edited safely", name, column)
                    continue
            else:
                connection.exec_driver_sql(
                    f"ALTER TABLE {preparer.format_table(table)} "
                    f"ALTER COLUMN {preparer.quote(column)} DROP NOT NULL"
                )
            relaxed.append(f"{name}.{column}")
    return relaxed
