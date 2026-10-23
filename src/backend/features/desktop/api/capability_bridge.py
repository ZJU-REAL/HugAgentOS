"""capability bridge responsibilities."""
from __future__ import annotations
from fastapi import APIRouter
from features.desktop.gateway.observability import ObservedGatewayRoute
router = APIRouter(prefix="/v1/desktop/capability", tags=["Desktop Capability"], route_class=ObservedGatewayRoute)
import asyncio
from core.auth.backend import UserContext, get_current_user
from core.infra.logging import quiet_access_log
from core.infra.responses import success_response
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field
import logging
logger = logging.getLogger(__name__)
from features.desktop.api import desktop_capability_state

class CloudBridgeBody(BaseModel):
    cloud_base: str = Field(..., min_length=1, description="云端后端根地址（含协议）")
    token: str = Field(..., min_length=8, description="capability token")
    expires_in: int = Field(default=600, ge=60, le=600)
    device_id: str = Field(
        ..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    )


def require_desktop_bridge_process() -> None:
    from features.desktop.auth.bridge import bridge_enabled

    if not bridge_enabled():
        raise HTTPException(status_code=403, detail="仅桌面双端本机后端可用")


def require_desktop_shell_control(request: Request) -> None:
    from features.desktop.capability.service import is_desktop_shell_control

    require_desktop_bridge_process()
    if not is_desktop_shell_control(
        request.headers.get("authorization", ""), request.headers.get("origin")
    ):
        raise HTTPException(status_code=401, detail="desktop shell authorization required")


@router.post("/cloud-bridge", summary="推送云端能力桥配置（桌面壳 → 本机后端）")
async def set_cloud_bridge(
    body: CloudBridgeBody,
    _: None = Depends(require_desktop_shell_control),
):
    require_desktop_bridge_process()
    from features.desktop.bridge.client import set_state

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
async def clear_cloud_bridge(_: None = Depends(require_desktop_shell_control)):
    require_desktop_bridge_process()
    from features.desktop.bridge.identity import clear_state

    clear_state()
    return success_response(data={"ok": True})


@router.get("/cloud-bridge/status", summary="云端能力桥状态")
async def cloud_bridge_status(_user: UserContext = Depends(get_current_user)):
    from features.desktop.bridge.status import bridge_status

    return success_response(data=bridge_status())


@router.get("/cloud-bridge/readiness", summary="桌面壳首次能力同步进度")
@quiet_access_log
async def cloud_bridge_readiness(_: None = Depends(require_desktop_shell_control)):
    from features.desktop.bridge.status import initial_sync_status

    return success_response(data=await asyncio.to_thread(initial_sync_status))
