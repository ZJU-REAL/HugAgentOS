"""Authenticated stateless Streamable HTTP gateway for persisted query definitions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import re
from typing import Annotated, Any

from features.hosted_apps.data import ApplicationDataService
from features.hosted_apps.schema import ToolDefinition
from features.hosted_apps.store import applications
from fastapi import HTTPException
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from sqlalchemy import select
from starlette.responses import JSONResponse


def authorized_application(app_id: str, token: str) -> tuple[ApplicationDataService, dict]:
    service = ApplicationDataService()
    with service.engine.connect() as connection:
        row = (
            connection.execute(
                select(applications).where(
                    applications.c.id == app_id,
                )
            )
            .mappings()
            .first()
        )
    if (
        not token
        or not row
        or not row["mcp_enabled"]
        or not row["token_hash"]
        or not hmac.compare_digest(row["token_hash"], hashlib.sha256(token.encode()).hexdigest())
    ):
        raise HTTPException(401, "Invalid or revoked application credential")
    return service, dict(row)


def query_tool(service: ApplicationDataService, app: dict, definition: ToolDefinition):
    def query(**arguments: Any) -> dict:
        unknown = set(arguments) - set(definition.filters) - {"limit", "offset"}
        if unknown:
            raise ValueError("Unsupported query filters")
        return service.query(
            app["id"],
            app["user_id"],
            definition.table,
            filters={k: v for k, v in arguments.items() if k in definition.filters},
            fields=definition.fields,
            limit=arguments.get("limit", 50),
            offset=arguments.get("offset", 0),
        )

    # MCPServer needs an explicit typed signature rather than opaque **kwargs.
    from features.hosted_apps.relational import table_definition

    fields = {c.name: c for c in table_definition(app, definition.table).columns}
    types = {"text": str, "integer": int, "number": float, "boolean": bool, "date": str}
    parameters = [
        inspect.Parameter(
            name,
            inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=types[fields[name].type] | None,
        )
        for name in definition.filters
    ]
    parameters.extend(
        [
            inspect.Parameter("limit", inspect.Parameter.KEYWORD_ONLY, default=50, annotation=Annotated[int, Field(ge=1, le=100)]),
            inspect.Parameter("offset", inspect.Parameter.KEYWORD_ONLY, default=0, annotation=Annotated[int, Field(ge=0, le=100000)]),
        ]
    )

    async def invoke(**arguments) -> dict[str, Any]:
        arguments = {k: v for k, v in arguments.items() if v is not None}
        return await asyncio.to_thread(query, **arguments)

    invoke.__signature__ = inspect.Signature(parameters, return_annotation=dict[str, Any])
    invoke.__name__ = definition.name
    return invoke


class QueryMCPServer(MCPServer):
    async def call_tool(self, name, arguments, context=None):
        import jsonschema

        schemas = {tool.name: tool.input_schema for tool in await self.list_tools()}
        if name in schemas:
            schema = {**schemas[name], "additionalProperties": False}
            try:
                jsonschema.validate(arguments, schema)
            except jsonschema.ValidationError:
                raise ToolError("Invalid query arguments") from None
        return await super().call_tool(name, arguments, context)


class ApplicationMCPGateway:
    """Each HTTP call reloads persisted authorization, so revocation works across workers."""

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        if scope["method"] in {"GET", "DELETE", "HEAD"}:
            await JSONResponse({"detail": "This stateless MCP endpoint accepts POST"},
                               405, headers={"Allow": "POST, OPTIONS"})(scope, receive, send)
            return
        app_id = scope["path"].rstrip("/").split("/")[-1]
        if not re.fullmatch("[0-9a-f]{32}", app_id):
            await JSONResponse({"detail": "Application not found"}, 404)(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        authorization = headers.get(b"authorization", b"").decode("latin1")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        try:
            service, app = await asyncio.to_thread(authorized_application, app_id, token)
        except HTTPException as error:
            await JSONResponse(
                {"detail": error.detail}, error.status_code, headers={"WWW-Authenticate": "Bearer"}
            )(scope, receive, send)
            return
        server = QueryMCPServer(app["title"])
        for raw in app["tools"]:
            definition = ToolDefinition.model_validate(raw)
            server.add_tool(
                query_tool(service, app, definition),
                name=definition.name,
                description=definition.description,
                annotations=ToolAnnotations(
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
            )
        transport = server.streamable_http_app(host="0.0.0.0", stateless_http=True, json_response=True)
        inner_scope = {**scope, "path": "/mcp", "raw_path": b"/mcp", "root_path": ""}
        async with server.session_manager.run():
            await transport(inner_scope, receive, send)


from starlette.responses import Response


class ApplicationMCPResponse(Response):
    """Pass FastAPI's authenticated protocol endpoint to the SDK ASGI transport."""

    def __init__(self):
        super().__init__(content=b"")

    async def __call__(self, scope, receive, send):
        await ApplicationMCPGateway()(scope, receive, send)
