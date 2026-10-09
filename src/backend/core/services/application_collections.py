"""Opt-in public SQL collections with compare-and-swap replacement."""

import hashlib
import json
from uuid import uuid4

from core.infra.time import utc_now
from core.services.application_relational import json_record, table_definition, validated_row
from core.services.application_sql_policy import application_role
from core.services.application_store import applications, owned_application, write_connection
from fastapi import HTTPException
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError

MAX_COLLECTION = 1000


def snapshot(connection, table):
    rows = [
        json_record(row)
        for row in connection.execute(
            select(table).order_by(table.c.created_at, table.c.id).limit(MAX_COLLECTION + 1)
        ).mappings()
    ]
    if len(rows) > MAX_COLLECTION:
        raise HTTPException(409, "Collection exceeds the replacement limit")
    revision = hashlib.sha256(
        json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return {"items": rows, "revision": revision, "total": len(rows)}


def read_collection(service, app_id, owner, name):
    with service.engine.begin() as connection:
        app = owned_application(connection, app_id, owner)
        definition = table_definition(app, name)
        if not definition.public_read:
            raise HTTPException(404, "Public collection not found")
        with application_role(connection, app_id):
            table = service._table(app_id, definition)
            if definition.public_replace:
                return snapshot(connection, table)
            total = connection.scalar(select(func.count()).select_from(table))
            rows = connection.execute(
                select(table).order_by(table.c.created_at, table.c.id).limit(100)
            ).mappings()
            return {"items": [json_record(row) for row in rows], "total": total}


def replace_collection(service, app_id, owner, name, revision, rows):
    if (
        not isinstance(revision, str)
        or len(revision) != 64
        or not isinstance(rows, list)
        or len(rows) > MAX_COLLECTION
    ):
        raise HTTPException(422, "Invalid collection replacement")
    try:
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError()
        encoded = [len(json.dumps(row, allow_nan=False).encode()) for row in rows]
        if any(size > 16384 for size in encoded) or sum(encoded) > 524288:
            raise HTTPException(413, "Collection is too large")
    except (ValueError, TypeError):
        raise HTTPException(422, "Collection rows must be finite JSON objects")
    try:
        with write_connection(service.engine) as connection:
            connection.execute(
                select(applications.c.id).where(applications.c.id == app_id).with_for_update()
            ).first()
            app = owned_application(connection, app_id, owner)
            definition = table_definition(app, name)
            if not definition.public_read or not definition.public_replace:
                raise HTTPException(404, "Public collection not found")
            table = service._table(app_id, definition)
            values = [
                dict(
                    {field.name: None for field in definition.columns},
                    **validated_row(definition, row),
                    id=uuid4().hex,
                    version=1,
                    created_at=utc_now(),
                )
                for row in rows
            ]
            with application_role(connection, app_id):
                if snapshot(connection, table)["revision"] != revision:
                    raise HTTPException(409, "Collection changed; reload before saving")
                connection.execute(delete(table))
                if values:
                    connection.execute(insert(table), values)
                return snapshot(connection, table)
    except IntegrityError:
        raise HTTPException(409, "Collection constraint violation")
