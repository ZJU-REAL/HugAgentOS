"""capability tools responsibilities."""
from __future__ import annotations
from fastapi import APIRouter
from features.desktop.gateway.observability import ObservedGatewayRoute
router = APIRouter(prefix="/v1/desktop/capability", tags=["Desktop Capability"], route_class=ObservedGatewayRoute)
import asyncio
import time
from core.infra.responses import success_response
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
import logging
logger = logging.getLogger(__name__)
from features.desktop.api import capability_dependencies
from features.desktop.api import desktop_capability_state

class GatewayToolCallBody(BaseModel):
    tool_name: str = Field(..., min_length=1, max_length=200)
    arguments: dict = Field(default_factory=dict)
    schema_hash: str = Field(..., min_length=64, max_length=64)


@router.post("/gateway/{server_id}/site-publish", summary="上传桌面构建产物并云端托管")
async def gateway_site_publish(
    server_id: str,
    request: Request,
    user_id: str = Depends(capability_dependencies.require_capability_user),
):
    import tarfile

    from core.infra.exceptions import AppException
    from features.desktop.capability.service import resolve_gateway_tool
    from core.capabilities.manifest_protocol import CapabilityManifestStaleError
    from core.harness.registry.transfer_channels import MAX_UPLOAD_OPTIONS_CHARS, UPLOAD_OPTIONS_HEADER, UPLOAD_SCHEMA_HEADER, endpoint_plugin
    from core.harness.registry.transfer_channels import upload_receiver
    receiver = upload_receiver("site-publish")
    from pydantic import ValidationError

    try:
        resolved = await run_in_threadpool(
            resolve_gateway_tool,
            user_id,
            server_id,
            "publish_site",
            schema_hash=request.headers.get(UPLOAD_SCHEMA_HEADER, ""),
        )
    except CapabilityManifestStaleError:
        raise HTTPException(status_code=409, detail="capability manifest changed") from None
    if resolved is None or str(
        resolved["target"].get("source_plugin") or ""
    ) != endpoint_plugin("site-publish"):
        raise HTTPException(status_code=403, detail="site publishing is not authorized")
    raw_options = request.headers.get(UPLOAD_OPTIONS_HEADER, "{}")
    if len(raw_options) > MAX_UPLOAD_OPTIONS_CHARS:
        raise HTTPException(status_code=400, detail="site options too large")
    try:
        options = receiver.options_model.model_validate_json(raw_options)
    except ValidationError:
        raise HTTPException(status_code=400, detail="invalid site options") from None
    archive = bytearray()
    async for chunk in request.stream():
        if len(archive) + len(chunk) > receiver.max_bytes:
            raise HTTPException(status_code=413, detail="site archive exceeds 40 MB")
        archive.extend(chunk)
    try:
        result = await asyncio.to_thread(receiver.publish, user_id, bytes(archive), options)
    except (ValueError, tarfile.TarError):
        raise HTTPException(status_code=400, detail="invalid site build archive") from None
    except AppException as exc:
        raise HTTPException(status_code=400, detail=exc.message) from None
    return success_response(data=capability_dependencies.public_content(user_id, result))


@router.post(
    "/gateway/{server_id}/call",
    summary="桌面云端工具调用网关（动态 manifest schema + JSON invocation）",
)
async def gateway_mcp_call(
    server_id: str,
    body: GatewayToolCallBody,
    request: Request,
    user_id: str = Depends(capability_dependencies.require_capability_user),
):
    """Execute a currently-authorized MCP tool from the cloud network."""
    from features.desktop.capability.service import CapabilityContentRejected, invoke_gateway_tool, resolve_gateway_tool
    from core.capabilities.manifest_protocol import CapabilityManifestStaleError

    try:
        resolved = await run_in_threadpool(
            resolve_gateway_tool,
            user_id,
            server_id,
            body.tool_name,
            schema_hash=body.schema_hash,
        )
    except CapabilityManifestStaleError as exc:
        raise HTTPException(status_code=409, detail="capability manifest changed") from exc
    if resolved is None:
        raise HTTPException(status_code=404, detail="tool not available")

    runtime_headers = {
        k: v for k, v in request.headers.items() if k.lower() in desktop_capability_state.RUNTIME_CONTEXT_HEADERS
    }
    from core.harness.registry.tool_operations import RemoteOperationConflict
    started = time.monotonic()
    try:
        result = await invoke_gateway_tool(resolved, body.arguments, runtime_headers)
    except HTTPException:
        from extensions.mcp_servers.registry import gateway_server
        if gateway_server(resolved["server_id"]) is not None:
            raise
        raise HTTPException(502, "Cloud tool call failed") from None
    except RemoteOperationConflict:
        raise HTTPException(status_code=400, detail="operation key already bound to different arguments") from None
    except CapabilityContentRejected:
        raise HTTPException(
            status_code=422,
            detail={"code": "integrity_failed", "message": "upstream content blocked"},
        ) from None
    except asyncio.TimeoutError as exc:
        logger.warning(
            "[desktop-capability] tool call timeout user=%s server=%s tool=%s",
            user_id,
            server_id,
            body.tool_name,
        )
        raise HTTPException(status_code=504, detail="upstream mcp tool timed out") from exc
    except Exception as exc:  # noqa: BLE001 - never expose cloud credentials/errors
        logger.warning(
            "[desktop-capability] tool call failed user=%s server=%s tool=%s error_type=%s",
            user_id,
            server_id,
            body.tool_name,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="upstream mcp tool failed") from exc

    logger.info(
        "[desktop-capability] tool call user=%s server=%s tool=%s duration_ms=%.0f",
        user_id,
        server_id,
        body.tool_name,
        (time.monotonic() - started) * 1000,
    )
    return success_response(data=capability_dependencies.public_content(user_id, result))


class GatewayReceiptBody(BaseModel):
    tool_name: str = Field(..., min_length=1, max_length=128)
    schema_hash: str = Field(..., min_length=64, max_length=64)
    operation_id: str = Field(..., min_length=1, max_length=128)


@router.post("/gateway/{server_id}/authorize", summary="校验本机任务工具的当前授权")
async def gateway_authorize_local(
    server_id: str, body: GatewayReceiptBody,
    user_id: str = Depends(capability_dependencies.require_capability_user),
):
    from features.desktop.capability.service import resolve_gateway_tool
    from core.capabilities.manifest_protocol import CapabilityManifestStaleError
    from core.harness.registry.tool_operations import local_tool_allowed
    try:
        resolved = await run_in_threadpool(resolve_gateway_tool, user_id, server_id,
                                          body.tool_name, schema_hash=body.schema_hash)
    except CapabilityManifestStaleError:
        raise HTTPException(status_code=409, detail="capability manifest changed") from None
    if (resolved is None or not local_tool_allowed(
            resolved["target"].get("source_plugin", ""), body.tool_name)):
        raise HTTPException(status_code=403, detail="local automation is not authorized")
    return success_response(data={"authorized": True})


@router.post("/gateway/{server_id}/receipt", summary="查询云端操作回执")
async def gateway_effect_receipt(
    server_id: str, body: GatewayReceiptBody,
    user_id: str = Depends(capability_dependencies.require_capability_user),
):
    from features.desktop.capability.service import resolve_gateway_tool
    from core.capabilities.manifest_protocol import CapabilityManifestStaleError
    from core.harness.registry.tool_operations import receipt_operations

    try:
        resolved = await run_in_threadpool(resolve_gateway_tool, user_id, server_id,
                                          body.tool_name, schema_hash=body.schema_hash)
    except CapabilityManifestStaleError:
        raise HTTPException(status_code=409, detail="capability manifest changed") from None
    operations = receipt_operations(body.tool_name)
    if resolved is None or operations is None:
        raise HTTPException(status_code=404, detail="tool not available")
    result = await run_in_threadpool(operations.lookup, user_id, server_id,
                                    body.tool_name, body.operation_id)
    return success_response(data=capability_dependencies.public_content(user_id, result))
