"""Shared relational data operations for authenticated REST and hosted MCP."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from core.infra.time import utc_now
from core.services.application_relational import json_record, table_definition, validated_row
from core.services.application_schema import ApplicationDefinition, RecordBatch, TableDefinition
from core.services.application_sql_policy import (
    application_role,
    application_table,
    provision_table,
)
from core.services.application_store import (
    application_engine,
    application_sources,
    applications,
    operations,
    owned_application,
    require_store,
    write_connection,
)
from fastapi import HTTPException
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError


class ApplicationDataService:
    def __init__(self, engine=None):
        self.engine = engine or application_engine()
        require_store(self.engine)

    def _table(self, app_id, definition):
        return application_table(self.engine, app_id, definition)

    def create(self, owner: str, definition: ApplicationDefinition) -> dict:
        app_id = uuid4().hex
        with write_connection(self.engine) as connection:
            if definition.site_id:
                existing = (
                    connection.execute(
                        select(applications).where(
                            applications.c.site_id == definition.site_id,
                        )
                    )
                    .mappings()
                    .first()
                )
                if existing:
                    if existing["user_id"] != owner:
                        raise HTTPException(404, "Application not found")
                    return self.serialize(existing)
            if (
                connection.scalar(
                    select(func.count())
                    .select_from(applications)
                    .where(applications.c.user_id == owner)
                )
                >= 100
            ):
                raise HTTPException(409, "Application quota exceeded")
            app = dict(
                id=app_id,
                user_id=owner,
                title=definition.title,
                site_id=definition.site_id or None,
                tables={},
                tools=[],
                token_hash=None,
                mcp_enabled=False,
                mcp_version=0,
                created_at=utc_now(),
            )
            try:
                connection.execute(insert(applications).values(**app))
            except IntegrityError:
                raise HTTPException(409, "Application already exists")
        return self.serialize(app)

    def list(self, owner: str) -> list[dict]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(applications, application_sources.c.project_id)
                .outerjoin(application_sources, applications.c.id == application_sources.c.app_id)
                .where(
                    applications.c.user_id == owner,
                )
                .order_by(applications.c.created_at.desc())
                .limit(100)
            ).mappings()
            return [self.serialize(row) for row in rows]

    @staticmethod
    def serialize(app) -> dict:
        return {k: v for k, v in dict(app).items() if k not in {"token_hash", "user_id"}}

    def get(self, app_id: str, owner: str) -> dict:
        with self.engine.connect() as connection:
            app = self.serialize(owned_application(connection, app_id, owner))
            app["project_id"] = connection.scalar(
                select(application_sources.c.project_id).where(
                    application_sources.c.app_id == app_id,
                )
            )
            return app

    def define_table(self, app_id: str, owner: str, definition: TableDefinition) -> dict:
        with write_connection(self.engine) as connection:
            # Serialize provisioning across backend workers; PostgreSQL DDL is transactional.
            statement = (
                select(applications)
                .where(
                    applications.c.id == app_id,
                    applications.c.user_id == owner,
                )
                .with_for_update()
            )
            app = connection.execute(statement).mappings().first()
            if not app:
                raise HTTPException(404, "Application not found")
            tables = dict(app["tables"])
            if definition.name in tables:
                if tables[definition.name] != definition.model_dump():
                    raise HTTPException(409, "Existing table schema cannot be overwritten")
                return tables[definition.name]
            if len(tables) >= 30:
                raise HTTPException(409, "Application table quota exceeded")
            provision_table(connection, app_id, self._table(app_id, definition))
            tables[definition.name] = definition.model_dump()
            connection.execute(
                update(applications).where(applications.c.id == app_id).values(tables=tables)
            )
        return definition.model_dump()

    def insert(
        self, app_id: str, owner: str, name: str, batch: RecordBatch, *, public=False
    ) -> dict:
        try:
            with write_connection(self.engine) as connection:
                app = owned_application(connection, app_id, owner)
                definition = table_definition(app, name)
                if public and not definition.public_insert:
                    raise HTTPException(403, "Visitor submissions are disabled")
                table = self._table(app_id, definition)
                # Lock the registry row to serialize quotas and request-key insertion.
                connection.execute(
                    select(applications.c.id)
                    .where(
                        applications.c.id == app_id,
                    )
                    .with_for_update()
                ).first()
                digest = hashlib.sha256(
                    json.dumps(
                        {"table": name, "rows": batch.rows},
                        sort_keys=True,
                        allow_nan=False,
                    ).encode()
                ).hexdigest()
                if batch.request_key:
                    saved = (
                        connection.execute(
                            select(operations).where(
                                operations.c.app_id == app_id,
                                operations.c.request_key == batch.request_key,
                            )
                        )
                        .mappings()
                        .first()
                    )
                    if saved:
                        if saved["body_hash"] != digest:
                            raise HTTPException(409, "Request key already used for different data")
                        return saved["result"]
                with application_role(connection, app_id):
                    count = connection.scalar(select(func.count()).select_from(table))
                if count + len(batch.rows) > 100000:
                    raise HTTPException(409, "Application record quota exceeded")
                rows = [
                    dict(
                        validated_row(definition, row),
                        id=uuid4().hex,
                        version=1,
                        created_at=utc_now(),
                    )
                    for row in batch.rows
                ]
                with application_role(connection, app_id):
                    for row in rows:
                        connection.execute(insert(table).values(**row))
                result = {"items": [json_record(row) for row in rows]}
                if batch.request_key:
                    connection.execute(
                        insert(operations).values(
                            app_id=app_id,
                            request_key=batch.request_key,
                            body_hash=digest,
                            result=result,
                        )
                    )
                return result
        except IntegrityError:
            raise HTTPException(409, "Duplicate value or database constraint violation")

    def query(
        self, app_id: str, owner: str, name: str, *, filters=None, fields=None, limit=50, offset=0
    ) -> dict:
        if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
            raise HTTPException(422, "Invalid pagination")
        with self.engine.connect() as connection:
            app = owned_application(connection, app_id, owner)
            definition = table_definition(app, name)
            table = self._table(app_id, definition)
            if set(filters or {}) & {c.name for c in definition.columns if c.type == "json"}:
                raise HTTPException(422, "JSON columns are not filter parameters")
            filters = validated_row(definition, filters or {}, partial=True)
            selected = fields or list(table.c.keys())
            if not selected or set(selected) - set(table.c.keys()):
                raise HTTPException(422, "Unknown selected fields")
            predicates = [table.c[k] == v for k, v in filters.items()]
            with application_role(connection, app_id):
                total = connection.scalar(
                    select(func.count()).select_from(table).where(*predicates)
                )
                rows = connection.execute(
                    select(*[table.c[k] for k in selected])
                    .where(
                        *predicates,
                    )
                    .order_by(table.c.created_at, table.c.id)
                    .limit(limit)
                    .offset(offset)
                ).mappings()
                return {"items": [json_record(row) for row in rows], "total": total}

    def patch(
        self, app_id: str, owner: str, name: str, record_id: str, version: int, payload: dict
    ) -> dict:
        try:
            with write_connection(self.engine) as connection:
                app = owned_application(connection, app_id, owner)
                definition = table_definition(app, name)
                table = self._table(app_id, definition)
                with application_role(connection, app_id):
                    result = connection.execute(
                        update(table)
                        .where(
                            table.c.id == record_id,
                            table.c.version == version,
                        )
                        .values(
                            **validated_row(definition, payload, partial=True), version=version + 1
                        )
                    )
                    if result.rowcount != 1:
                        raise HTTPException(409, "Record changed or no longer exists")
                    row = (
                        connection.execute(select(table).where(table.c.id == record_id))
                        .mappings()
                        .one()
                    )
                    return json_record(row)
        except IntegrityError:
            raise HTTPException(409, "Database constraint violation")
