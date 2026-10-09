"""Owner deletion with publication serialization and complete quota reclamation."""
from fastapi import HTTPException
from sqlalchemy import delete, select, text, update
from core.services.application_store import applications, operations, application_sources, mcp_deployments, owned_application, write_connection
from core.services.application_history import collection_history
from core.services.application_relational import table_definition
from core.services.application_sql_policy import application_role
from core.services.application_publication import publication_lock

def delete_record(service, app_id, owner, name, record_id, version):
    with write_connection(service.engine) as connection:
        connection.execute(select(applications.c.id).where(applications.c.id == app_id).with_for_update())
        app = owned_application(connection, app_id, owner)
        table = service._table(app_id, table_definition(app, name))
        with application_role(connection, app_id):
            result = connection.execute(delete(table).where(table.c.id == record_id, table.c.version == version))
        if result.rowcount != 1:
            raise HTTPException(409, "Record changed or no longer exists")
        # Owner erasure must also remove retained copies of the erased record.
        connection.execute(delete(collection_history).where(
            collection_history.c.app_id == app_id, collection_history.c.table_name == name))
        connection.execute(delete(operations).where(operations.c.app_id == app_id))
    return {"deleted": True}

def delete_table(service, app_id, owner, name):
    with publication_lock(service.engine, app_id), write_connection(service.engine) as connection:
        connection.execute(select(applications.c.id).where(applications.c.id == app_id).with_for_update())
        app = owned_application(connection, app_id, owner)
        definition = table_definition(app, name)
        if any(tool["table"] == name for tool in app["tools"]):
            raise HTTPException(409, "Remove this table from the MCP definition before deletion")
        service._table(app_id, definition).drop(connection)
        tables = dict(app["tables"]); tables.pop(name)
        connection.execute(update(applications).where(applications.c.id == app_id).values(tables=tables))
        connection.execute(delete(collection_history).where(
            collection_history.c.app_id == app_id, collection_history.c.table_name == name))
        connection.execute(delete(operations).where(operations.c.app_id == app_id))
    return {"deleted": True}

def delete_application(service, db, app_id, owner):
    from core.db.models import AdminMcpServer
    from core.services.mcp_management_service import refresh_mcp_caches
    with publication_lock(service.engine, app_id):
        with write_connection(service.engine) as connection:
            connection.execute(select(applications.c.id).where(applications.c.id == app_id).with_for_update())
            app = owned_application(connection, app_id, owner)
            # Revoke durably before touching the independently committed platform projection.
            connection.execute(update(applications).where(applications.c.id == app_id).values(
                token_hash=None, mcp_enabled=False))
        row = db.get(AdminMcpServer, "amcp_" + app_id)
        if row:
            if row.owner_user_id != owner or (row.extra_config or {}).get("hosted_app_id") != app_id:
                raise HTTPException(409, "Personal MCP identifier is already in use")
            db.delete(row); db.commit(); refresh_mcp_caches()
        with write_connection(service.engine) as connection:
            app = owned_application(connection, app_id, owner)
            if connection.dialect.name == "postgresql":
                # Identity has been loaded from the registry, never caller supplied SQL.
                connection.execute(text(f'DROP SCHEMA IF EXISTS "app_{app["id"]}" CASCADE'))
                connection.execute(text(f'DROP ROLE IF EXISTS "app_role_{app["id"]}"'))
            else:
                for raw in app["tables"].values():
                    from core.services.application_schema import TableDefinition
                    service._table(app_id, TableDefinition.model_validate(raw)).drop(connection)
            for table in (operations, mcp_deployments, application_sources, collection_history):
                connection.execute(delete(table).where(table.c.app_id == app_id))
            connection.execute(delete(applications).where(applications.c.id == app_id))
    # Source projects remain user-owned files; deletion never destroys an unrelated project.
    return {"deleted": True}
