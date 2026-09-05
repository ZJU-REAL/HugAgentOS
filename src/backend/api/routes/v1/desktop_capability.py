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
- cloud-bridge 接收端复用 require_config（桌面壳持有本机实例的 CONFIG_TOKEN
  即桥接秘密），且仅在桌面桥进程（HUGAGENT_DESKTOP_BRIDGE_SECRET 已注入）
  下开放，云端部署恒 403。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import httpx
from api.deps import require_config
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


@router.post("/token", summary="签发桌面能力令牌")
async def issue_token(user: UserContext = Depends(get_current_user)):
    """桌面壳用云端登录会话换取短时 capability token，再下发给本机后端。"""
    from core.services.desktop_capability import issue_capability_token

    data = issue_capability_token(str(user.user_id))
    logger.info("[desktop-capability] token issued for user=%s", user.user_id)
    return success_response(data=data)


# ── capability token 鉴权依赖 ──────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


def _require_capability_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> str:
    from core.services.desktop_capability import verify_capability_token

    token = credentials.credentials if credentials else ""
    user_id = verify_capability_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="invalid capability token")
    return user_id


# ── manifest（云端，capability token 鉴权） ─────────────────────────────


@router.get("/manifest", summary="当前用户的云端能力清单")
async def get_manifest(
    request: Request,
    response: Response,
    user_id: str = Depends(_require_capability_user),
):
    from core.services.desktop_capability import build_user_capability_manifest

    manifest = build_user_capability_manifest(user_id)
    etag = f'"{manifest["revision"]}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return success_response(data=manifest)


@router.get("/models", summary="桌面本机执行面的无密钥模型清单")
async def get_model_manifest(user_id: str = Depends(_require_capability_user)):
    from core.services.desktop_capability import build_user_model_manifest

    return success_response(data=build_user_model_manifest(user_id))


@router.get("/skills/manifest", summary="当前用户的云端技能清单")
async def get_skill_manifest(
    request: Request,
    response: Response,
    user_id: str = Depends(_require_capability_user),
):
    from core.services.desktop_capability import build_user_skill_manifest

    manifest = build_user_skill_manifest(user_id)
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

    resolved = resolve_skill_bundle(user_id, skill_id)
    if resolved is None:
        raise HTTPException(status_code=404, detail="skill not available")
    data, content_hash = resolved
    etag = f'"{content_hash}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=data, media_type="application/zip", headers=headers)


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
    from core.services.desktop_capability import invoke_gateway_tool, resolve_gateway_tool
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
    return success_response(data=result)


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
            "[desktop-capability] model gateway upstream error user=%s provider=%s: %s",
            user_id,
            provider_id,
            exc,
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
        upstream.aiter_raw(),
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
    client = _client()
    upstream_req = client.build_request(
        request.method, target["url"], headers=headers, content=body
    )
    try:
        upstream = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        logger.warning(
            "[desktop-capability] gateway upstream error user=%s server=%s: %s",
            user_id,
            server_id,
            exc,
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
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        headers=resp_headers,
        background=BackgroundTask(upstream.aclose),
    )


# ── 本机侧：壳推送桥配置 ────────────────────────────────────────────────


class CloudBridgeBody(BaseModel):
    cloud_base: str = Field(..., min_length=1, description="云端后端根地址（含协议）")
    token: str = Field(..., min_length=8, description="capability token")
    expires_in: int = Field(default=86400, ge=60, le=7 * 86400)


def _require_desktop_bridge_process() -> None:
    from core.auth.desktop_bridge import bridge_enabled

    if not bridge_enabled():
        raise HTTPException(status_code=403, detail="仅桌面双端本机后端可用")


@router.post("/cloud-bridge", summary="推送云端能力桥配置（桌面壳 → 本机后端）")
async def set_cloud_bridge(
    body: CloudBridgeBody,
    _: None = Depends(require_config),
):
    _require_desktop_bridge_process()
    from core.services.desktop_cloud_bridge import set_state

    base = body.cloud_base.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="cloud_base 必须是 http(s) 地址")
    set_state(base, body.token, body.expires_in)
    logger.info("[cloud-bridge] 桥配置已更新（cloud_base=%s）", base)
    return success_response(data={"ok": True})


@router.get("/cloud-bridge/status", summary="云端能力桥状态")
async def cloud_bridge_status(_user: UserContext = Depends(get_current_user)):
    from core.services.desktop_cloud_bridge import bridge_status

    return success_response(data=bridge_status())
