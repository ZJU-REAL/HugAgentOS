"""capability dependencies responsibilities."""
from __future__ import annotations
from typing import Optional
import httpx
from core.infra.responses import success_response
from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
import logging
logger = logging.getLogger(__name__)
from features.desktop.api import desktop_capability_state

def client() -> httpx.AsyncClient:
    pass  # State is owned by its responsibility module.
    if desktop_capability_state.gateway_client is None:
        desktop_capability_state.gateway_client = httpx.AsyncClient(
            timeout=httpx.Timeout(600.0),
            # 桌面端两条消息之间常常隔着几十秒；默认 5s 的 keepalive 意味着几乎
            # 每次模型调用都要重新 TCP + TLS 握手。
            limits=httpx.Limits(max_keepalive_connections=64, keepalive_expiry=300.0),
        )
    return desktop_capability_state.gateway_client


async def require_capability_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(desktop_capability_state.bearer),
) -> str:
    from features.desktop.capability.service import CAPABILITY_DEVICE_HEADER, verify_capability_token

    token = credentials.credentials if credentials else ""
    user_id = await verify_capability_token(
        token,
        device_id=request.headers.get(CAPABILITY_DEVICE_HEADER, ""),
        issuer=str(request.base_url),
    )
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid capability token")
    request.state.desktop_observation_user = user_id
    return user_id


def public_content(user_id: str, value, *, bundle: bool = False):
    from features.desktop.capability.service import CapabilityContentRejected, guard_capability_bundle, guard_capability_content

    try:
        if callable(value):
            value = value()
        return (
            guard_capability_bundle(user_id, value)
            if bundle
            else guard_capability_content(user_id, value)
        )
    except CapabilityContentRejected:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "integrity_failed",
                "message": "capability content blocked by credential policy",
            },
        ) from None


def stream_secrets(user_id: str, target: dict) -> set[str]:
    from features.desktop.capability.service import CapabilityContentRejected, gateway_stream_secrets

    try:
        return gateway_stream_secrets(user_id, target)
    except CapabilityContentRejected:
        raise HTTPException(
            status_code=422,
            detail={"code": "integrity_failed", "message": "credential policy unavailable"},
        ) from None


async def checked_upstream_bytes(upstream: httpx.Response, secrets: set[str]):
    from features.desktop.capability.service import CapabilityContentRejected, guard_capability_stream

    try:
        async for chunk in guard_capability_stream(upstream.aiter_raw(), secrets):
            yield chunk
    except CapabilityContentRejected:
        logger.warning("[desktop-capability] upstream content blocked code=integrity_failed")
        # Streaming headers may already be sent. Return only a fixed diagnostic;
        # the buffered bytes containing the credential are never released.
        yield b'data: {"error":{"code":"integrity_failed","message":"upstream content blocked"}}\n\n'
    finally:
        await upstream.aclose()


def entity_manifest_response(request: Request, response: Response, manifest: dict):
    etag = f'"{manifest["revision"]}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return success_response(data=manifest)


def bundle_response(request: Request, resolved):
    if resolved is None:
        raise HTTPException(status_code=404, detail="not available")
    data, content_hash = resolved
    etag = f'"{content_hash}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type="application/zip", headers=headers)
