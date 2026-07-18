"""Inbound channel bots: /v1/channels/*

Owner service-account model: a user self-binds one external IM bot (Lark preferred);
the bot runs with that user's identity + permissions.
Self CRUD + bind testing require login; the webhook entry is a public endpoint
(relies on channel signature verification, no session).

See internal design docs.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth.backend import UserContext, get_current_user
from core.channels.protocol import ChannelCaps
from core.channels.registry import get_adapter, list_adapters
from core.db.engine import get_db
from core.infra.responses import created_response, success_response
from core.services.channel_service import ChannelService, bot_to_dict

router = APIRouter(prefix="/v1/channels", tags=["Channels"])


# ── Pydantic ────────────────────────────────────────────────────────────
class _ResourceScope(BaseModel):
    kb_ids: Optional[List[str]] = None
    skill_ids: Optional[List[str]] = None


class CreateBotRequest(BaseModel):
    channel_type: str = Field("lark", description="渠道类型")
    app_id: str = Field(..., description="应用 App ID", max_length=128)
    app_secret: str = Field(..., description="应用 App Secret")
    encrypt_key: Optional[str] = Field(None, description="飞书 Encrypt Key（webhook 加密时填）")
    verification_token: Optional[str] = Field(None, description="飞书 Verification Token（webhook 模式填）")
    # Per-channel extra credentials (e.g. WeCom agent_id/token/aes_key): key -> plaintext value;
    # backend encrypts each into config individually.
    extra: Optional[Dict[str, str]] = Field(None, description="渠道附加凭据（按 credential_fields 提供）")
    display_name: Optional[str] = Field(None, max_length=100)
    transport: str = Field("long_conn", description="long_conn | webhook")
    resource_scope: Optional[_ResourceScope] = None
    # Bind to a specific sub-agent (passed when binding from the "sub-agent page"): inbound
    # messages are pinned to that sub-agent; omitted / null -> main agent (owner's default
    # capabilities); the "my bot" binding uses this path.
    agent_id: Optional[str] = Field(None, description="绑定的子智能体 ID；空 = 主智能体")


class UpdateBotRequest(BaseModel):
    display_name: Optional[str] = Field(None, max_length=100)
    enabled: Optional[bool] = None
    resource_scope: Optional[_ResourceScope] = None
    # Rebind only when this key is explicitly passed (including passing null to unbind);
    # untouched if omitted. Distinguished via model_fields_set.
    agent_id: Optional[str] = Field(None, description="改绑的子智能体 ID；null = 解绑回主智能体")


def _caps_to_dict(caps: ChannelCaps) -> Dict[str, Any]:
    return {
        "channel_type": caps.channel_type,
        "max_message_len": caps.max_message_len,
        "supports_markdown": caps.supports_markdown,
        "supports_long_conn": caps.supports_long_conn,
        "bind_mode": getattr(caps, "bind_mode", "credentials"),
        "credential_fields": list(getattr(caps, "credential_fields", ("app_id", "app_secret"))),
    }


# ── Meta info: supported channels ────────────────────────────────────────
@router.get("/adapters", summary="支持的渠道类型及能力")
async def list_channel_adapters():
    out: List[Dict[str, Any]] = []
    for ct in list_adapters():
        try:
            out.append(_caps_to_dict(get_adapter(ct).caps))
        except Exception:  # noqa: BLE001
            continue
    return success_response(data={"adapters": out})


# ── My bots CRUD ─────────────────────────────────────────────────────────
@router.get("/bots", summary="我的机器人列表")
async def list_my_bots(
    agent_id: Optional[str] = None,
    main_only: bool = False,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """``agent_id`` → 仅列绑该子智能体的 bot（子智能体页面用）；``main_only`` → 仅列主智能体
    bot（设置「我的机器人」用）；都不传 → 全部。"""
    svc = ChannelService(db)
    bots = svc.list_bots(str(user.user_id), agent_id=agent_id, main_only=main_only)
    return success_response(data={"bots": [bot_to_dict(b) for b in bots]})


@router.get("/conversations", summary="我的渠道会话（供定时投递选择目标）")
async def list_my_conversations(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出本人渠道 bot 已产生的会话（群/单聊），供 Web 后台配置定时投递目标。"""
    from core.services.channel_service import list_owner_conversations
    return success_response(data={"conversations": list_owner_conversations(db, str(user.user_id))})


@router.post("/bots", status_code=status.HTTP_201_CREATED, summary="绑定新机器人")
async def create_bot(
    body: CreateBotRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc = ChannelService(db)
    conn = await svc.create_bot(
        str(user.user_id),
        channel_type=body.channel_type,
        app_id=body.app_id,
        app_secret=body.app_secret,
        encrypt_key=body.encrypt_key,
        verification_token=body.verification_token,
        extra_credentials=body.extra,
        display_name=body.display_name,
        transport=body.transport,
        resource_scope=body.resource_scope.model_dump() if body.resource_scope else None,
        agent_id=body.agent_id,
    )
    data = bot_to_dict(conn)
    # In webhook mode, echo back the callback address for the user to fill into the channel backend.
    if conn.transport == "webhook":
        data["webhook_path"] = f"/v1/channels/{conn.channel_id}/webhook"
    return created_response(data=data)


@router.patch("/bots/{channel_id}", summary="更新机器人（启停 / 名称 / 资源白名单）")
async def update_bot(
    channel_id: str,
    body: UpdateBotRequest,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc = ChannelService(db)
    conn = svc.update_bot(
        str(user.user_id),
        channel_id,
        display_name=body.display_name,
        enabled=body.enabled,
        resource_scope=body.resource_scope.model_dump() if body.resource_scope else None,
        resource_scope_set=body.resource_scope is not None,
        agent_id=body.agent_id,
        agent_id_set="agent_id" in body.model_fields_set,
    )
    return success_response(data=bot_to_dict(conn))


@router.delete("/bots/{channel_id}", summary="删除机器人并清除凭据")
async def delete_bot(
    channel_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc = ChannelService(db)
    svc.delete_bot(str(user.user_id), channel_id)
    return success_response(data={"deleted": True})


@router.post("/bots/{channel_id}/test", summary="测试机器人凭据连通性")
async def test_bot(
    channel_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc = ChannelService(db)
    data = await svc.test_bot(str(user.user_id), channel_id)
    return success_response(data=data)


# ── WeChat QR-code binding (qr mode) ──────────────────────────────────────
@router.post("/weixin/bind/start", summary="微信扫码绑定：取二维码")
async def weixin_bind_start(
    agent_id: Optional[str] = None,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc = ChannelService(db)
    data = await svc.start_weixin_bind(str(user.user_id), agent_id=agent_id)
    return success_response(data=data)


@router.get("/weixin/bind/{bind_id}/status", summary="微信扫码绑定：轮询扫码状态")
async def weixin_bind_status(
    bind_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    svc = ChannelService(db)
    data = await svc.poll_weixin_bind(str(user.user_id), bind_id)
    return success_response(data=data)


# ── webhook entry (public endpoint, relies on channel signature verification) ──
@router.get("/{channel_id}/webhook", summary="渠道 webhook URL 校验（公开，企业微信用）")
async def channel_webhook_verify(
    channel_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """企业微信配置回调 URL 时发 GET 带 echostr → 核签后裸文本回明文。"""
    params = {k: v for k, v in request.query_params.items()}
    svc = ChannelService(db)
    echo = svc.handle_webhook_get(channel_id, params)
    return PlainTextResponse(content=echo)


@router.post("/{channel_id}/webhook", summary="渠道 webhook 入口（公开）")
async def channel_webhook(
    channel_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    body = await request.body()
    # Signature params: Lark uses headers (x-lark-*), WeCom uses the query string
    # (msg_signature/timestamp/nonce) -> merge both into one lowercased dict passed to
    # adapter.verify_webhook.
    headers = {k.lower(): v for k, v in request.headers.items()}
    for k, v in request.query_params.items():
        headers[k.lower()] = v
    svc = ChannelService(db)
    result = svc.handle_webhook(channel_id, headers, body)
    # The webhook response must be the bare JSON the channel expects (e.g. Lark url_verification's
    # challenge), not wrapped in an envelope.
    return JSONResponse(content=result)
