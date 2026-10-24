"""capability mcp gateway responsibilities."""
from __future__ import annotations
from fastapi import APIRouter
from features.desktop.gateway.observability import ObservedGatewayRoute
router = APIRouter(prefix="/v1/desktop/capability", tags=["Desktop Capability"], route_class=ObservedGatewayRoute)
import httpx
from fastapi import Depends, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse
import logging
logger = logging.getLogger(__name__)
from features.desktop.api import capability_dependencies
from features.desktop.api import desktop_capability_state

@router.api_route(
    "/gateway/{server_id}/mcp",
    methods=["POST", "GET", "DELETE"],
    summary="桌面能力网关（MCP streamable-http 反代）",
)
async def gateway_mcp(
    server_id: str,
    request: Request,
    user_id: str = Depends(capability_dependencies.require_capability_user),
):
    # 授权解析走 30s per-user 缓存、内部短会话——不经 Depends(get_db)，
    # 避免流式转发全程占住连接池里的一条 DB 连接。
    from features.desktop.capability.service import resolve_gateway_target

    target = await run_in_threadpool(resolve_gateway_target, user_id, server_id)
    if target is None:
        raise HTTPException(status_code=404, detail="server not available")

    headers: dict[str, str] = {
        k: v for k, v in request.headers.items() if k.lower() not in desktop_capability_state.DROP_REQUEST_HEADERS
    }
    # 云端侧连接凭据（远程第三方 MCP 的 headers 等）只在此处物化，不出云端。
    for k, v in (target.get("headers") or {}).items():
        if isinstance(k, str) and isinstance(v, str):
            headers[k] = v
    # 身份以 capability token 为准，客户端注入的同名头已在 deny-list 拦下。
    headers["X-Current-User-Id"] = user_id
    # StreamingResponse 使用 aiter_raw 原样转发，因此不允许上游压缩后再丢失
    # Content-Encoding；这是旧客户端透明 MCP 端点的传输完整性要求。
    headers["accept-encoding"] = "identity"

    body = await request.body()
    response_secrets = await run_in_threadpool(capability_dependencies.stream_secrets, user_id, target)
    client = capability_dependencies.client()
    upstream_req = client.build_request(
        request.method, target["url"], headers=headers, content=body
    )
    try:
        upstream = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        logger.warning(
            "[desktop-capability] gateway upstream error user=%s server=%s error_type=%s",
            user_id,
            server_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="upstream mcp unreachable")

    logger.info(
        "[desktop-capability] gateway %s user=%s server=%s status=%s",
        request.method,
        user_id,
        server_id,
        upstream.status_code,
    )
    resp_headers = {
        name: upstream.headers[name] for name in desktop_capability_state.FWD_RESPONSE_HEADERS if name in upstream.headers
    }
    return StreamingResponse(
        capability_dependencies.checked_upstream_bytes(upstream, response_secrets),
        status_code=upstream.status_code,
        headers=resp_headers,
        background=BackgroundTask(upstream.aclose),
    )
