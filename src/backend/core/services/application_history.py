"""Bounded, owner-readable recovery snapshots for public collection replacement."""
import json
from uuid import uuid4
from core.infra.time import utc_now
from core.services.application_store import metadata, owned_application
from fastapi import HTTPException
from sqlalchemy import Boolean, Column, DateTime, JSON, String, Table, delete, insert, select

collection_history = Table(
    "hosted_collection_history", metadata,
    Column("id", String(32), primary_key=True),
    Column("app_id", String(32), nullable=False, index=True),
    Column("table_name", String(48), nullable=False),
    Column("rows", JSON, nullable=False),
    Column("protected", Boolean, nullable=False, server_default="false"),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

def archive(connection, app_id, name, rows):
    if len(json.dumps(rows, ensure_ascii=False).encode()) > 524288:
        raise HTTPException(409, "Recovery snapshot exceeds 512 KiB; use owner record operations")
    protected = connection.scalar(select(collection_history.c.id).where(
        collection_history.c.app_id == app_id, collection_history.c.table_name == name,
        collection_history.c.protected.is_(True)))
    pin = bool(rows) and protected is None
    connection.execute(insert(collection_history).values(
        id=uuid4().hex, app_id=app_id, table_name=name, rows=rows, protected=pin, created_at=utc_now()))
    retained = list(connection.scalars(select(collection_history.c.id).where(
        collection_history.c.app_id == app_id, collection_history.c.table_name == name,
        collection_history.c.protected.is_(False)
    ).order_by(collection_history.c.created_at.desc(), collection_history.c.id.desc()).offset(19 if protected or pin else 20)))
    if retained:
        connection.execute(delete(collection_history).where(collection_history.c.id.in_(retained)))

def list_history(service, app_id, owner, name):
    from core.services.application_relational import table_definition, json_record
    with service.engine.connect() as connection:
        table_definition(owned_application(connection, app_id, owner), name)
        rows = connection.execute(select(
            collection_history.c.id, collection_history.c.created_at
        ).where(collection_history.c.app_id == app_id, collection_history.c.table_name == name)
          .order_by(collection_history.c.created_at.desc(), collection_history.c.id.desc())).mappings()
        return {"items": [json_record(row) for row in rows]}

def restore(service, app_id, owner, name, history_id, revision):
    from core.services.application_collections import replace_collection
    from core.services.application_relational import table_definition
    with service.engine.connect() as connection:
        definition = table_definition(owned_application(connection, app_id, owner), name)
        rows = connection.scalar(select(collection_history.c.rows).where(
            collection_history.c.id == history_id, collection_history.c.app_id == app_id,
            collection_history.c.table_name == name))
    if rows is None:
        raise HTTPException(404, "Recovery snapshot not found")
    fields = {column.name for column in definition.columns}
    return replace_collection(service, app_id, owner, name, revision,
                              [{k: v for k, v in row.items() if k in fields} for row in rows],
                              owner_restore=True)
