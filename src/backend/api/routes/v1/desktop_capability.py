"""桌面双端能力桥 API（云端侧 + 本机侧共用一个路由文件，各端点自行守门）。

云端侧（部署在云端 / 预发 / 生产后端）：
  POST /v1/desktop/capability/token                  会话换取短时 capability token
  GET  /v1/desktop/capability/manifest               当前用户最终可用 MCP 清单
  POST /v1/desktop/capability/gateway/{sid}/call     动态 manifest 的 JSON 工具调用网关
  ANY  /v1/desktop/capability/gateway/{sid}/mcp      已安装旧客户端使用的透明反代网关
  GET  /v1/desktop/capability/models                 无密钥模型拓扑
  POST /v1/desktop/capability/gateway/models/...     模型流式反代网关
  GET  /v1/desktop/capability/skills/manifest        当前用户最终可用技能清单（含内容哈希）
  GET  /v1/desktop/capability/skills/{id}/bundle     单个授权技能的完整 zip 包

本机侧（桌面壳孵化的本机后端）：
  POST /v1/desktop/capability/cloud-bridge           壳推送 {cloud_base, token}
  GET  /v1/desktop/capability/cloud-bridge/status    桥诊断视图

安全模型：
- token 端点要求云端登录会话；网关/manifest 只认 capability token（HMAC 短时
  令牌，见 core/services/desktop_capability.py），会话 cookie / 内部 token /
  第三方密钥不出云端；
- 网关按「该用户当前有效能力集」授权 server_id，未命中一律 404（不区分
  不存在/无权）；身份头由网关覆写，客户端伪造的 X-Current-User-Id 不生效；
- cloud-bridge 接收端只认桌面壳 Bearer 进程秘密，不以用户管理权限替代；
  仅在桌面桥进程（HUGAGENT_DESKTOP_BRIDGE_SECRET 已注入）下开放，
  浏览器 Origin 请求拒绝，云端部署恒 403。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import httpx
from core.auth.backend import UserContext, get_current_user
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/desktop/capability", tags=["Desktop Capability"])

# 网关上行连接：连接短超时快速失败；读不设限（SSE 长流 / 长工具调用），
# 上游 MCP 自身带 execution_timeout 兜底。
_gateway_client: Optional[httpx.AsyncClient] = None


def _client() -> httpx.AsyncClient:
    global _gateway_client
    if _gateway_client is None:
        _gateway_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0),
        )
    return _gateway_client


# ── token 签发（云端，会话鉴权） ────────────────────────────────────────


class CapabilityTokenBody(BaseModel):
    device_id: str = Field(
        ..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    )


@router.post("/token", summary="签发桌面能力令牌")
async def issue_token(body: CapabilityTokenBody, request: Request, response: Response):
    """Only an actual live cloud cookie session can mint a desktop credential."""
    import hashlib

    from core.auth.session import validate_session
    from core.config.settings import settings
    from core.services.desktop_capability import issue_capability_token, session_authorization_epoch

    cookie = request.cookies.get(settings.session.cookie_name, "")
    try:
        current = await validate_session(cookie) if cookie else None
    except Exception:
        current = None
    if (
        not current
        or not current.get("user_id")
        or not current.get("user_center_id")
        or not session_authorization_epoch(current)
    ):
        raise HTTPException(status_code=401, detail="valid cloud session required")
    data = issue_capability_token(
        str(current["user_id"]),
        device_id=body.device_id,
        issuer=str(request.base_url),
        session_hash=hashlib.sha256(cookie.encode()).hexdigest(),
        authorization_epoch=session_authorization_epoch(current),
        user_center_id=str(current["user_center_id"]),
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return success_response(data=data)


# ── capability token 鉴权依赖 ──────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


async def _require_capability_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> str:
    from core.services.desktop_capability import CAPABILITY_DEVICE_HEADER, verify_capability_token

    token = credentials.credentials if credentials else ""
    user_id = await verify_capability_token(
        token,
        device_id=request.headers.get(CAPABILITY_DEVICE_HEADER, ""),
        issuer=str(request.base_url),
    )
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid capability token")
    return user_id


def _public_content(user_id: str, value, *, bundle: bool = False):
    from core.services.desktop_capability import (
        CapabilityContentRejected,
        guard_capability_bundle,
        guard_capability_content,
    )

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


def _stream_secrets(user_id: str, target: dict) -> set[str]:
    from core.services.desktop_capability import CapabilityContentRejected, gateway_stream_secrets

    try:
        return gateway_stream_secrets(user_id, target)
    except CapabilityContentRejected:
        raise HTTPException(
            status_code=422,
            detail={"code": "integrity_failed", "message": "credential policy unavailable"},
        ) from None


async def _checked_upstream_bytes(upstream: httpx.Response, secrets: set[str]):
    from core.services.desktop_capability import CapabilityContentRejected, guard_capability_stream

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


# ── manifest（云端，capability token 鉴权） ─────────────────────────────


@router.get("/manifest", summary="当前用户的云端能力清单")
async def get_manifest(
    request: Request,
    response: Response,
    user_id: str = Depends(_require_capability_user),
):
    from core.services.desktop_capability import build_user_capability_manifest

    manifest = _public_content(user_id, lambda: build_user_capability_manifest(user_id))
    etag = f'"{manifest["revision"]}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return success_response(data=manifest)


@router.get("/models", summary="桌面本机执行面的无密钥模型清单")
async def get_model_manifest(user_id: str = Depends(_require_capability_user)):
    from core.services.desktop_capability import build_user_model_manifest

    return success_response(
        data=_public_content(user_id, lambda: build_user_model_manifest(user_id))
    )


@router.get("/skills/manifest", summary="当前用户的云端技能清单")
async def get_skill_manifest(
    request: Request,
    response: Response,
    user_id: str = Depends(_require_capability_user),
):
    from core.services.desktop_capability import build_user_skill_manifest

    manifest = _public_content(user_id, lambda: build_user_skill_manifest(user_id))
    etag = f'"{manifest["revision"]}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return success_response(data=manifest)


@router.get("/skills/{skill_id}/bundle", summary="下载一个当前授权技能的完整 zip 包")
async def get_skill_bundle(
    skill_id: str,
    request: Request,
    user_id: str = Depends(_require_capability_user),
):
    from core.services.desktop_capability import resolve_skill_bundle

    resolved = _public_content(
        user_id, lambda: resolve_skill_bundle(user_id, skill_id), bundle=True
    )
    if resolved is None:
        raise HTTPException(status_code=404, detail="skill not available")
    data, content_hash = resolved
    etag = f'"{content_hash}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type="application/zip", headers=headers)


def _entity_manifest_response(request: Request, response: Response, manifest: dict):
    etag = f'"{manifest["revision"]}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return success_response(data=manifest)


def _bundle_response(request: Request, resolved):
    if resolved is None:
        raise HTTPException(status_code=404, detail="not available")
    data, content_hash = resolved
    etag = f'"{content_hash}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type="application/zip", headers=headers)


@router.get("/agents/manifest", summary="当前用户可见的智能体定义清单")
async def get_agent_manifest(
    request: Request, response: Response, user_id: str = Depends(_require_capability_user)
):
    from core.services.desktop_capability import build_user_agent_manifest

    return _entity_manifest_response(
        request, response, _public_content(user_id, lambda: build_user_agent_manifest(user_id))
    )


@router.get(
    "/agents/{agent_id}/bundle", summary="下载一个智能体定义包（agent.json + instructions.md）"
)
async def get_agent_bundle(
    agent_id: str, request: Request, user_id: str = Depends(_require_capability_user)
):
    from core.services.desktop_capability import resolve_agent_bundle

    return _bundle_response(
        request,
        _public_content(user_id, lambda: resolve_agent_bundle(user_id, agent_id), bundle=True),
    )


@router.get("/plugins/manifest", summary="当前用户已安装插件清单")
async def get_plugin_manifest(
    request: Request, response: Response, user_id: str = Depends(_require_capability_user)
):
    from core.services.desktop_capability import build_user_plugin_manifest

    return _entity_manifest_response(
        request, response, _public_content(user_id, lambda: build_user_plugin_manifest(user_id))
    )


@router.get("/plugins/{install_id}/bundle", summary="下载一个插件定义包（plugin.json）")
async def get_plugin_bundle(
    install_id: str, request: Request, user_id: str = Depends(_require_capability_user)
):
    from core.services.desktop_capability import resolve_plugin_bundle

    return _bundle_response(
        request,
        _public_content(user_id, lambda: resolve_plugin_bundle(user_id, install_id), bundle=True),
    )


# ── MCP 网关（云端，capability token 鉴权，透明反代） ──────────────────

# 请求侧按 deny-list 透传：运行时上下文头（X-Chat-Id、X-Reranker-Enabled、
# 外接 KB 头等，见 agent_factory._inject_runtime_headers）随本机侧注入原样
# 过桥，新增上下文头无需改网关；只拦截身份/凭据/逐跳头，身份由网关按
# capability token 覆写。
_DROP_REQUEST_HEADERS = {
    # 身份与凭据：capability token 不上传上游；身份头由网关重写
    "authorization",
    "cookie",
    "x-current-user-id",
    # 知识库授权按云端用户重算（头缺失时 KB MCP 自动解析可用知识库）
    "x-allowed-kb-ids",
    # 桌面桥内部头
    "x-desktop-bridge",
    "x-desktop-bridge-user",
    "x-desktop-device-id",
    "x-hugagent-target",
    # 逐跳 / 传输层头，由 httpx 按新连接重建
    "host",
    "content-length",
    "connection",
    "accept-encoding",
    "transfer-encoding",
    "keep-alive",
    "proxy-authorization",
    "te",
    "upgrade",
}
# 响应侧透传的头。
_FWD_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "mcp-protocol-version")
_RUNTIME_CONTEXT_HEADERS = {
    "x-chat-id",
    "x-channel-id",
    "x-conversation-id",
    "x-allowed-dataset-ids",
    "x-reranker-enabled",
}


class GatewayToolCallBody(BaseModel):
    tool_name: str = Field(..., min_length=1, max_length=200)
    arguments: dict = Field(default_factory=dict)
    schema_hash: str = Field(..., min_length=64, max_length=64)


@router.post("/gateway/{server_id}/site-publish", summary="上传桌面构建产物并云端托管")
async def gateway_site_publish(
    server_id: str,
    request: Request,
    user_id: str = Depends(_require_capability_user),
):
    import tarfile

    from core.infra.exceptions import AppException
    from core.services.desktop_capability import component_base_name, resolve_gateway_tool
    from core.services.desktop_capability_protocol import CapabilityManifestStaleError
    from core.services.desktop_gateway_uploads import (
        MAX_UPLOAD_OPTIONS_CHARS,
        UPLOAD_OPTIONS_HEADER,
        UPLOAD_SCHEMA_HEADER,
        endpoint_component,
    )
    from core.services.desktop_site_publish import (
        MAX_ARCHIVE_BYTES,
        SiteUploadOptions,
        publish_uploaded_site,
    )
    from pydantic import ValidationError

    try:
        resolved = resolve_gateway_tool(
            user_id,
            server_id,
            "publish_site",
            schema_hash=request.headers.get(UPLOAD_SCHEMA_HEADER, ""),
        )
    except CapabilityManifestStaleError:
        raise HTTPException(status_code=409, detail="capability manifest changed") from None
    if resolved is None or component_base_name(
        server_id,
        resolved["target"].get("source_plugin"),
        resolved["target"].get("owner_user_id"),
    ) != endpoint_component("site-publish"):
        raise HTTPException(status_code=403, detail="site publishing is not authorized")
    raw_options = request.headers.get(UPLOAD_OPTIONS_HEADER, "{}")
    if len(raw_options) > MAX_UPLOAD_OPTIONS_CHARS:
        raise HTTPException(status_code=400, detail="site options too large")
    try:
        options = SiteUploadOptions.model_validate_json(raw_options)
    except ValidationError:
        raise HTTPException(status_code=400, detail="invalid site options") from None
    archive = bytearray()
    async for chunk in request.stream():
        if len(archive) + len(chunk) > MAX_ARCHIVE_BYTES:
            raise HTTPException(status_code=413, detail="site archive exceeds 40 MB")
        archive.extend(chunk)
    try:
        result = await asyncio.to_thread(publish_uploaded_site, user_id, bytes(archive), options)
    except (ValueError, tarfile.TarError):
        raise HTTPException(status_code=400, detail="invalid site build archive") from None
    except AppException as exc:
        raise HTTPException(status_code=400, detail=exc.message) from None
    return success_response(data=_public_content(user_id, result))


@router.post(
    "/gateway/{server_id}/call",
    summary="桌面云端工具调用网关（动态 manifest schema + JSON invocation）",
)
async def gateway_mcp_call(
    server_id: str,
    body: GatewayToolCallBody,
    request: Request,
    user_id: str = Depends(_require_capability_user),
):
    """Execute a currently-authorized MCP tool from the cloud network."""
    from core.services.desktop_capability import (
        CapabilityContentRejected,
        invoke_gateway_tool,
        resolve_gateway_tool,
    )
    from core.services.desktop_capability_protocol import CapabilityManifestStaleError

    try:
        resolved = resolve_gateway_tool(
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
        k: v for k, v in request.headers.items() if k.lower() in _RUNTIME_CONTEXT_HEADERS
    }
    started = time.monotonic()
    try:
        result = await invoke_gateway_tool(resolved, body.arguments, runtime_headers)
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
    return success_response(data=_public_content(user_id, result))


@router.post(
    "/gateway/models/{provider_id}/{model_path:path}",
    summary="桌面模型网关（OpenAI-compatible 流式反代）",
)
async def gateway_model(
    provider_id: str,
    model_path: str,
    request: Request,
    user_id: str = Depends(_require_capability_user),
):
    """把本机 Agent 的对话/向量/重排请求转发到云端内网模型。

    路径、模型名和上游凭据全由云端 DB 重算；客户端提供的
    Authorization / x-api-key / model 字段都不可信。
    """
    from core.services.desktop_capability import resolve_model_gateway_target

    target = resolve_model_gateway_target(user_id, provider_id)
    if target is None or model_path.strip("/") != target["path"]:
        raise HTTPException(status_code=404, detail="model not available")

    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="model request must be JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="model request must be a JSON object")
    payload["model"] = target["model_name"]

    headers = {
        "authorization": f"Bearer {target['api_key']}",
        "content-type": "application/json",
        "accept": request.headers.get("accept", "application/json"),
        # aiter_raw 保留原始字节，因此明确要求上游不压缩，避免在没有
        # Content-Encoding 响应头的情况下把 gzip 字节直接送给 OpenAI SDK。
        "accept-encoding": "identity",
    }
    response_secrets = _stream_secrets(user_id, target)
    client = _client()
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
        _checked_upstream_bytes(upstream, response_secrets),
        status_code=upstream.status_code,
        headers=response_headers,
        background=BackgroundTask(upstream.aclose),
    )


@router.api_route(
    "/gateway/{server_id}/mcp",
    methods=["POST", "GET", "DELETE"],
    summary="桌面能力网关（MCP streamable-http 反代）",
)
async def gateway_mcp(
    server_id: str,
    request: Request,
    user_id: str = Depends(_require_capability_user),
):
    # 授权解析走 30s per-user 缓存、内部短会话——不经 Depends(get_db)，
    # 避免流式转发全程占住连接池里的一条 DB 连接。
    from core.services.desktop_capability import resolve_gateway_target

    target = resolve_gateway_target(user_id, server_id)
    if target is None:
        raise HTTPException(status_code=404, detail="server not available")

    headers: dict[str, str] = {
        k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST_HEADERS
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
    response_secrets = _stream_secrets(user_id, target)
    client = _client()
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
        name: upstream.headers[name] for name in _FWD_RESPONSE_HEADERS if name in upstream.headers
    }
    return StreamingResponse(
        _checked_upstream_bytes(upstream, response_secrets),
        status_code=upstream.status_code,
        headers=resp_headers,
        background=BackgroundTask(upstream.aclose),
    )


# ── 本机侧：壳推送桥配置 ────────────────────────────────────────────────


class CloudBridgeBody(BaseModel):
    cloud_base: str = Field(..., min_length=1, description="云端后端根地址（含协议）")
    token: str = Field(..., min_length=8, description="capability token")
    expires_in: int = Field(default=600, ge=60, le=600)
    device_id: str = Field(
        ..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    )


def _require_desktop_bridge_process() -> None:
    from core.auth.desktop_bridge import bridge_enabled

    if not bridge_enabled():
        raise HTTPException(status_code=403, detail="仅桌面双端本机后端可用")


def _require_desktop_shell_control(request: Request) -> None:
    from core.services.desktop_capability import is_desktop_shell_control

    _require_desktop_bridge_process()
    if not is_desktop_shell_control(
        request.headers.get("authorization", ""), request.headers.get("origin")
    ):
        raise HTTPException(status_code=401, detail="desktop shell authorization required")


@router.post("/cloud-bridge", summary="推送云端能力桥配置（桌面壳 → 本机后端）")
async def set_cloud_bridge(
    body: CloudBridgeBody,
    _: None = Depends(_require_desktop_shell_control),
):
    _require_desktop_bridge_process()
    from core.services.desktop_cloud_bridge import set_state

    base = body.cloud_base.strip().rstrip("/")
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="cloud_base 地址无效")
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(status_code=400, detail="cloud_base 必须是不含凭据的 http(s) 地址")
    set_state(base, body.token, body.expires_in, device_id=body.device_id)
    logger.info("[cloud-bridge] 桥配置已更新（cloud_base=%s）", base)
    return success_response(data={"ok": True})


@router.delete("/cloud-bridge", summary="清除云端能力桥（桌面退出登录）")
async def clear_cloud_bridge(_: None = Depends(_require_desktop_shell_control)):
    _require_desktop_bridge_process()
    from core.services.desktop_cloud_bridge import clear_state

    clear_state()
    return success_response(data={"ok": True})


@router.get("/cloud-bridge/status", summary="云端能力桥状态")
async def cloud_bridge_status(_user: UserContext = Depends(get_current_user)):
    from core.services.desktop_cloud_bridge import bridge_status

    return success_response(data=bridge_status())
