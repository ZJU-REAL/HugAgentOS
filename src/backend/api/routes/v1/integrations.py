"""Third-party account integrations: /v1/integrations/*

Currently provides DingTalk account connection (for the dingtalk skill / dws CLI). Device-flow OAuth:
the frontend first POSTs ``/login`` to obtain a verification URL + user_code to present to the user; after the
user approves in DingTalk, the frontend polls ``/login/poll`` until connected. Credential persistence and
orchestration are in core/services/dingtalk_service.py and
internal design docs.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.infra.responses import success_response
from core.services.dingtalk_service import DingTalkService
from core.services.email_service import EmailService
from core.services.lark_service import LarkService
from core.services.yida_service import YidaService

router = APIRouter(prefix="/v1/integrations", tags=["Integrations"])


@router.get("/dingtalk/status", summary="查询钉钉连接状态")
async def dingtalk_status(
    probe: bool = Query(False, description="true 时到沙箱实时核对登录态并对账"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc = DingTalkService(db)
    data = await svc.probe_status(str(user.user_id)) if probe else svc.get_status(str(user.user_id))
    return success_response(data=data)


@router.post("/dingtalk/login", summary="发起钉钉设备流登录")
async def dingtalk_login(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc = DingTalkService(db)
    data = await svc.start_device_login(str(user.user_id))
    return success_response(data=data)


@router.post("/dingtalk/login/poll", summary="轮询钉钉登录是否完成")
async def dingtalk_login_poll(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc = DingTalkService(db)
    data = await svc.poll_login(str(user.user_id))
    return success_response(data=data)


@router.post("/dingtalk/disconnect", summary="断开钉钉连接并清除凭据")
async def dingtalk_disconnect(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc = DingTalkService(db)
    data = await svc.disconnect(str(user.user_id))
    return success_response(data=data)


# ── Lark account connection (feishu-cli plugin / lark-cli): scan-QR device flow, same structure as DingTalk ──

@router.get("/lark/status", summary="查询飞书连接状态")
async def lark_status(
    probe: bool = Query(False, description="true 时真实 API 探活并对账"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc = LarkService(db)
    data = await svc.probe_status(str(user.user_id)) if probe else svc.get_status(str(user.user_id))
    return success_response(data=data)


@router.post("/lark/login", summary="发起飞书设备流登录（扫码）")
async def lark_login(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc = LarkService(db)
    data = await svc.start_device_login(str(user.user_id))
    return success_response(data=data)


@router.post("/lark/login/poll", summary="轮询飞书扫码登录是否完成")
async def lark_login_poll(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc = LarkService(db)
    data = await svc.poll_login(str(user.user_id))
    return success_response(data=data)


@router.post("/lark/disconnect", summary="断开飞书连接并清除凭据")
async def lark_disconnect(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc = LarkService(db)
    data = await svc.disconnect(str(user.user_id))
    return success_response(data=data)


# ── Yida account connection (yida plugin / openyida CLI): scan-QR login executed via the user's sandbox ──
# Unlike DingTalk/Lark — the backend does not install Node; start/poll both run openyida inside that user's sandbox
# (the sandbox pre-installs the CLI + mounts a persistent working directory); no DB table, the cookie file is the
# source of truth for the connection. For multi-org accounts, poll returns corp_selection + organizations; the
# frontend lets the user pick, then re-polls with corp_id.

class _YidaPollRequest(BaseModel):
    corp_id: Optional[str] = None


@router.get("/yida/status", summary="查询宜搭连接状态")
async def yida_status(
    user: UserContext = Depends(get_current_user),
):
    data = YidaService().get_status(str(user.user_id))
    return success_response(data=data)


@router.post("/yida/login", summary="发起宜搭扫码登录（借用户沙箱出二维码）")
async def yida_login(
    user: UserContext = Depends(get_current_user),
):
    data = await YidaService().start_login(str(user.user_id))
    return success_response(data=data)


@router.post("/yida/login/poll", summary="轮询宜搭扫码登录是否完成（多组织时带 corp_id 选组织）")
async def yida_login_poll(
    body: Optional[_YidaPollRequest] = None,
    user: UserContext = Depends(get_current_user),
):
    data = await YidaService().poll_login(str(user.user_id), corp_id=(body.corp_id if body else None))
    return success_response(data=data)


@router.post("/yida/disconnect", summary="断开宜搭连接并清除登录态")
async def yida_disconnect(
    user: UserContext = Depends(get_current_user),
):
    data = await YidaService().disconnect(str(user.user_id))
    return success_response(data=data)


# ── Email account connection (email plugin / himalaya CLI): IMAP/SMTP auth code, synchronous binding ──
# Unlike DingTalk/Lark — email has no device flow / no QR code / no OAuth; binding is synchronous "save form → validate",
# so there is no /login/poll; the connection is completed by POST /connect submitting the credential form.

class _EmailServerOverrides(BaseModel):
    imap_host: Optional[str] = None
    imap_port: Optional[int] = None
    imap_security: Optional[str] = None  # tls | starttls | none
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_security: Optional[str] = None


class _EmailConnectRequest(BaseModel):
    email_address: str
    secret: str                          # IMAP/SMTP auth code / app password
    display_name: Optional[str] = None
    server_overrides: Optional[_EmailServerOverrides] = None


@router.get("/email/status", summary="查询电子邮箱连接状态")
async def email_status(
    probe: bool = Query(False, description="true 时真实连通性探活并对账"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc = EmailService(db)
    data = await svc.probe_status(str(user.user_id)) if probe else svc.get_status(str(user.user_id))
    return success_response(data=data)


@router.post("/email/connect", summary="绑定电子邮箱（保存凭据并同步校验）")
async def email_connect(
    body: _EmailConnectRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc = EmailService(db)
    data = await svc.connect(
        str(user.user_id),
        email_address=body.email_address,
        secret=body.secret,
        display_name=body.display_name,
        server_overrides=(body.server_overrides.model_dump() if body.server_overrides else None),
    )
    return success_response(data=data)


@router.post("/email/disconnect", summary="断开电子邮箱连接并清除凭据")
async def email_disconnect(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc = EmailService(db)
    data = await svc.disconnect(str(user.user_id))
    return success_response(data=data)
