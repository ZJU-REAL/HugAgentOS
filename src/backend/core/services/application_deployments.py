"""Persisted MCP configuration, credential rotation and rollback."""

from __future__ import annotations

import hashlib

from core.infra.time import utc_now
from core.services.application_relational import table_definition
from core.services.application_schema import ToolDefinition
from core.services.application_store import (
    application_engine,
    applications,
    mcp_deployments,
    owned_application,
    require_store,
    write_connection,
)
from fastapi import HTTPException
from sqlalchemy import insert, select, update


class ApplicationDeploymentService:
    def __init__(self, engine=None):
        self.engine = engine or application_engine()
        require_store(self.engine)

    def publish_mcp(self, app_id: str, owner: str, tools: list[ToolDefinition]) -> dict:
        import secrets

        if not tools or len(tools) > 30 or len({t.name for t in tools}) != len(tools):
            raise HTTPException(422, "Provide 1–30 distinctly named tools")
        token = secrets.token_urlsafe(32)
        with write_connection(self.engine) as connection:
            row = (
                connection.execute(
                    select(applications)
                    .where(
                        applications.c.id == app_id,
                        applications.c.user_id == owner,
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if not row:
                raise HTTPException(404, "Application not found")
            app = dict(row)
            version = app["mcp_version"] + 1
            for tool in tools:
                definition = table_definition(app, tool.table)
                if set(tool.filters) & {c.name for c in definition.columns if c.type == "json"}:
                    raise HTTPException(422, "JSON columns are not MCP filter parameters")
                allowed = {c.name for c in definition.columns}
                if set(tool.fields + tool.filters) - allowed:
                    raise HTTPException(422, "MCP fields must exist in the selected table")
            connection.execute(
                update(applications)
                .where(applications.c.id == app_id)
                .values(
                    tools=[tool.model_dump() for tool in tools],
                    mcp_version=version,
                    token_hash=hashlib.sha256(token.encode()).hexdigest(),
                    mcp_enabled=True,
                )
            )
            connection.execute(
                insert(mcp_deployments).values(
                    app_id=app_id,
                    version=version,
                    tools=[tool.model_dump() for tool in tools],
                    created_at=utc_now(),
                )
            )
        return {
            "url": f"/applications-mcp/{app_id}",
            "token": token,
            "version": version,
            "transport": "streamable-http",
            "tools": [t.name for t in tools],
        }

    def revoke(self, app_id: str, owner: str) -> dict:
        with write_connection(self.engine) as connection:
            owned_application(connection, app_id, owner)
            connection.execute(
                update(applications)
                .where(applications.c.id == app_id)
                .values(
                    token_hash=None,
                    mcp_enabled=False,
                )
            )
        return {"ok": True}

    def rollback_mcp(self, app_id: str, owner: str, version: int) -> dict:
        with self.engine.connect() as connection:
            owned_application(connection, app_id, owner)
            tools = connection.scalar(
                select(mcp_deployments.c.tools).where(
                    mcp_deployments.c.app_id == app_id,
                    mcp_deployments.c.version == version,
                )
            )
            if tools is None:
                raise HTTPException(404, "MCP deployment not found")
        return self.publish_mcp(app_id, owner, [ToolDefinition.model_validate(t) for t in tools])
