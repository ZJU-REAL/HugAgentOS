"""桌面双端能力桥 API（云端侧 + 本机侧共用一个路由文件，各端点自行守门）。

云端侧（部署在云端 / 预发 / 生产后端）：
  POST /v1/desktop/capability/token                  会话换取短时 capability token
  GET  /v1/desktop/capability/manifest               当前用户最终可用 MCP 清单
  ANY  /v1/desktop/capability/gateway/{sid}/mcp      MCP streamable-http 透明反代网关

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

import logging
from typing import Optional

import httpx
from api.deps import require_config
from core.auth.backend import UserContext, get_current_user
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, HTTPException, Request
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
async def get_manifest(user_id: str = Depends(_require_capability_user)):
    from core.services.desktop_capability import build_user_capability_manifest

    return success_response(data=build_user_capability_manifest(user_id))


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

    body = await request.body()
    client = _client()
    upstream_req = client.build_request(request.method, target["url"], headers=headers, content=body)
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
