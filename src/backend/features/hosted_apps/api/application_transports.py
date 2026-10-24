"""Agent callbacks, visitor submissions and the hosted MCP protocol endpoint."""

import json

from core.infra.responses import success_response
from features.hosted_apps.data import ApplicationDataService
from features.hosted_apps.operations import perform_operation
from features.hosted_apps.schema import InternalMCPPublish, InternalOperation, RecordBatch
from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from core.db.threads import run_in_session

internal_router = APIRouter(prefix="/v1/internal/applications", tags=["internal-applications"])
public_router = APIRouter(prefix="/site", tags=["application-data"])
mcp_router = APIRouter(prefix="/applications-mcp", tags=["hosted-mcp"])


@internal_router.post("/operation")
async def internal_operation(
    body: InternalOperation,
    x_internal_token: str | None = Header(None),
):
    from features.sites.api.internal_site_auth import _check_internal_token

    _check_internal_token(x_internal_token)
    from core.harness.registry.host_runtime import requires_remote_execution as bridge_enabled

    if bridge_enabled():
        from features.sites.desktop_publish import forward_local_site_tool

        arguments = body.model_dump(exclude={"user_id", "chat_id"})
        if not arguments["payload"]:
            arguments["payload"] = None
        return success_response(
            data=await forward_local_site_tool(
                "manage_application",
                arguments,
                user_id=body.user_id,
                chat_id=body.chat_id,
            )
        )
    result = await run_in_session(lambda db: perform_operation(body, db))
    return success_response(data=result)


@internal_router.post("/publish-mcp")
async def internal_publish_mcp(
    body: InternalMCPPublish,
    x_internal_token: str | None = Header(None),
):
    from features.sites.api.internal_site_auth import _check_internal_token
    from core.harness.registry.host_runtime import requires_remote_execution as bridge_enabled

    _check_internal_token(x_internal_token)
    if bridge_enabled():
        from features.sites.desktop_publish import forward_local_site_tool

        result = await forward_local_site_tool(
            "publish_mcp",
            body.model_dump(exclude={"user_id", "chat_id"}),
            user_id=body.user_id,
            chat_id=body.chat_id,
        )
    else:
        from features.hosted_apps.projects import ApplicationProjectService
        from features.hosted_apps.publication import ApplicationPublicationService

        result = await run_in_session(
            lambda db: ApplicationPublicationService(db).publish(
                body.app_id,
                body.user_id,
                body.tools,
                install_personal=True,
                chat_id=body.chat_id,
            )
        )

    return success_response(data=result)


@public_router.post("/{slug}/__api/data/{table}")
async def visitor_insert(slug: str, table: str, request: Request):
    from features.sites.api.sites_serve import _api_json, _client_ip, _load_authorized_site
    from features.hosted_apps.store import applications

    from features.sites.rate_limit import count_attempt
    await count_attempt(_client_ip(request), slug)
    site, gate = await _load_authorized_site(slug, request, require_unlock=True)
    if gate is not None:
        return _api_json(
            {"detail": "Site access authorization is required"},
            gate.status_code if gate.status_code >= 400 else 401,
        )
    # Bound actual incoming bytes even when Content-Length is absent.
    body_bytes = bytearray()
    async for chunk in request.stream():
        body_bytes.extend(chunk)
        if len(body_bytes) > 16384:
            raise HTTPException(413, "Submission is too large")
    try:
        payload = json.loads(body_bytes)
        batch = RecordBatch.model_validate(payload)
        if len(batch.rows) != 1:
            raise ValueError("Visitor submissions accept one record")
    except (ValueError, TypeError):
        raise HTTPException(422, "Invalid submission")
    service = ApplicationDataService()

    def submit():
        with service.engine.connect() as connection:
            app = (
                connection.execute(
                    select(applications).where(
                        applications.c.site_id == site.site_id,
                    )
                )
                .mappings()
                .first()
            )
            if not app:
                raise HTTPException(404, "Application not found")
            result = service.insert(app["id"], app["user_id"], table, batch, public=True)
            return {"ok": True, "id": result["items"][0]["id"]}

    return _api_json(await run_in_threadpool(submit))


@public_router.api_route("/{slug}/__api/data/{table}", methods=["GET", "PUT"])
async def visitor_collection(slug: str, table: str, request: Request):
    from features.sites.api.sites_serve import _api_json, _client_ip, _load_authorized_site
    from features.hosted_apps.collections import read_collection, replace_collection
    from features.hosted_apps.store import applications

    site, gate = await _load_authorized_site(slug, request, require_unlock=True)
    if gate is not None:
        return _api_json(
            {"detail": "Site access authorization is required"},
            gate.status_code if gate.status_code >= 400 else 401,
        )
    payload = None
    if request.method == "PUT":
        from features.sites.rate_limit import count_attempt
        await count_attempt(_client_ip(request), slug)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 524288:
                raise HTTPException(413, "Collection is too large")
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict) or set(payload) != {"revision", "rows"}:
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(422, "Invalid collection")
    service = ApplicationDataService()

    def operation():
        with service.engine.connect() as connection:
            app = (
                connection.execute(
                    select(applications).where(applications.c.site_id == site.site_id)
                )
                .mappings()
                .first()
            )
        if not app:
            raise HTTPException(404, "Application not found")
        if payload is None:
            return read_collection(service, app["id"], app["user_id"], table)
        return replace_collection(
            service, app["id"], app["user_id"], table, payload["revision"], payload["rows"]
        )

    return _api_json(await run_in_threadpool(operation))


@mcp_router.api_route("/{app_id}", methods=["GET", "POST", "DELETE", "OPTIONS"])
async def hosted_mcp(app_id: str, request: Request):
    from features.hosted_apps.mcp import ApplicationMCPResponse

    return ApplicationMCPResponse()
