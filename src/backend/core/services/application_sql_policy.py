"""PostgreSQL application schemas and least-privilege data-operation roles."""

from __future__ import annotations

from contextlib import contextmanager

from core.services.application_relational import sql_table
from sqlalchemy import text


def application_table(engine, app_id, definition):
    table = sql_table(app_id, definition)
    if engine.dialect.name == "postgresql":
        table.schema = "app_" + app_id
        table.name = definition.name
    return table


def provision_table(connection, app_id, table):
    if connection.dialect.name == "postgresql":
        schema = table.schema
        role = "app_role_" + app_id
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        exists = connection.scalar(
            text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
        )
        if not exists:
            connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
            connection.execute(text(f'GRANT "{role}" TO CURRENT_USER'))
        table.create(connection)
        connection.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"'))
        connection.execute(
            text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{schema}"."{table.name}" TO "{role}"')
        )
    else:
        table.create(connection)


@contextmanager
def application_role(connection, app_id):
    if connection.dialect.name != "postgresql":
        yield
        return
    if len(app_id) != 32 or any(c not in "0123456789abcdef" for c in app_id):
        raise ValueError("Invalid application identity")
    connection.execute(text(f'SET LOCAL ROLE "app_role_{app_id}"'))
    try:
        yield
    finally:
        # Skip reset on an aborted transaction: the surrounding transaction rolls back.
        from sqlalchemy.exc import DBAPIError

        try:
            connection.execute(text("RESET ROLE"))
        except DBAPIError:
            pass
