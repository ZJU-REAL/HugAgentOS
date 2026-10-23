"""capability model gateway responsibilities."""
from __future__ import annotations
from fastapi import APIRouter
from features.desktop.gateway.observability import ObservedGatewayRoute
router = APIRouter(prefix="/v1/desktop/capability", tags=["Desktop Capability"], route_class=ObservedGatewayRoute)
import json
import httpx
from fastapi import Depends, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse
import logging
logger = logging.getLogger(__name__)
from features.desktop.api import capability_dependencies
from features.desktop.api import desktop_capability_state

@router.post(
    "/gateway/models/{provider_id}/{model_path:path}",
    summary="桌面模型网关（OpenAI-compatible 流式反代）",
)
async def gateway_model(
    provider_id: str,
    model_path: str,
    request: Request,
    user_id: str = Depends(capability_dependencies.require_capability_user),
):
    """把本机 Agent 的对话/向量/重排请求转发到云端内网模型。

    路径、模型名和上游凭据全由云端 DB 重算；客户端提供的
    Authorization / x-api-key / model 字段都不可信。
    """
    from features.desktop.capability.service import resolve_model_gateway_target

    target = await run_in_threadpool(resolve_model_gateway_target, user_id, provider_id)
    if target is None or model_path.strip("/") != target["path"]:
        raise HTTPException(status_code=404, detail="model not available")

    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="model request must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="model request must be a JSON object")
    payload["model"] = target["model_name"]
    request.state.desktop_observation_model = target["model_name"]

    headers = {
        "authorization": f"Bearer {target['api_key']}",
        "content-type": "application/json",
        "accept": request.headers.get("accept", "application/json"),
        # aiter_raw 保留原始字节，因此明确要求上游不压缩，避免在没有
        # Content-Encoding 响应头的情况下把 gzip 字节直接送给 OpenAI SDK。
        "accept-encoding": "identity",
    }
    response_secrets = await run_in_threadpool(capability_dependencies.stream_secrets, user_id, target)
    client = capability_dependencies.client()
    upstream_req = client.build_request(
        "POST",
        target["url"],
        headers=headers,
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )
    try:
        upstream = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        logger.warning(
            "[desktop-capability] model gateway upstream error user=%s provider=%s error_type=%s",
            user_id,
            provider_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="upstream model unreachable")

    logger.info(
        "[desktop-capability] model gateway user=%s provider=%s type=%s status=%s",
        user_id,
        provider_id,
        target["provider_type"],
        upstream.status_code,
    )
    response_headers = {}
    if "content-type" in upstream.headers:
        response_headers["content-type"] = upstream.headers["content-type"]
    return StreamingResponse(
        capability_dependencies.checked_upstream_bytes(upstream, response_secrets),
        status_code=upstream.status_code,
        headers=response_headers,
        background=BackgroundTask(upstream.aclose),
    )
