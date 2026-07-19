"""Community-edition authentication routes.

CE keeps local account login, cookie sessions, and desktop handoff, but does
not ship the enterprise remote SSO/OA integration surface.
"""

from __future__ import annotations

from typing import Optional

from core.auth import desktop_ticket_store
from core.auth.capabilities import page_admin_flags, resolve_capabilities
from core.auth.mock_ticket_store import consume_ticket
from core.auth.session import (
    create_session,
    expires_at_iso,
    remember_ttl_seconds,
    revoke_session,
    session_cookie_params,
    validate_session,
)
from core.config.settings import settings
from core.db.engine import get_db
from core.infra.logging import get_logger
from core.infra.responses import success_response
from core.services import UserService
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

logger = get_logger(__name__)
router = APIRouter(prefix="/v1/auth", tags=["Auth"])


class TicketExchangeRequest(BaseModel):
    code: Optional[str] = None


class DesktopRedeemRequest(BaseModel):
    ticket: str = Field(..., min_length=1)


def _login_url() -> str:
    return settings.sso.effective_login_url or settings.sso.login_url or "/login"


def _set_session_cookie(response: Response, token: str, ttl_seconds: Optional[int] = None) -> None:
    response.set_cookie(value=token, **session_cookie_params(ttl_seconds))


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=settings.session.cookie_name, path="/")


def _serialize_user(db: Session, user_data: dict, ttl_seconds: Optional[int] = None) -> dict:
    from core.db.models import LocalUser, UserShadow

    user_id = user_data["user_id"]
    shadow = db.query(UserShadow).filter(UserShadow.user_id == user_id).first()
    local = db.query(LocalUser).filter(LocalUser.user_id == user_id).first()
    meta = dict(shadow.extra_data or {}) if shadow else {}
    caps = resolve_capabilities(meta)
    return {
        "user_id": user_id,
        "username": shadow.username if shadow else user_data.get("username", ""),
        "email": shadow.email if shadow else user_data.get("email"),
        "avatar_url": shadow.avatar_url if shadow else user_data.get("avatar_url"),
        "nickname": local.nickname if local else user_data.get("nickname"),
        "real_name": local.real_name if local else user_data.get("real_name"),
        "department": None,
        "teams": [],
        "expires_at": expires_at_iso(ttl_seconds or user_data.get("ttl_seconds")),
        "sso_token": None,
        "must_change_password": bool(meta.get("must_change_password")),
        **caps,
        **page_admin_flags(meta, caps),
    }


@router.post("/ticket/exchange", summary="本地登录票据换取会话")
async def ticket_exchange(
    body: TicketExchangeRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    credential = (body.code or "").strip()
    user_info = consume_ticket(credential) if credential else None
    if user_info is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": 30002,
                "message": "Invalid or expired ticket",
                "data": {"login_url": _login_url()},
            },
        )

    shadow = UserService(db).get_or_create_user_shadow(
        user_center_id=user_info["user_center_id"],
        username=user_info["username"],
        email=user_info.get("email"),
        avatar_url=user_info.get("avatar_url"),
    )
    ttl_seconds = remember_ttl_seconds() if user_info.get("remember") else None
    session_data = {
        "user_id": shadow.user_id,
        "user_center_id": shadow.user_center_id,
        "username": shadow.username,
        "email": shadow.email,
        "avatar_url": shadow.avatar_url,
        "nickname": user_info.get("nickname"),
        "real_name": user_info.get("real_name"),
    }
    token = await create_session(session_data, ttl_seconds=ttl_seconds)
    _set_session_cookie(response, token, ttl_seconds)
    return success_response(
        data=_serialize_user(db, session_data, ttl_seconds),
        message="Login successful",
    )


@router.get("/sso/authorize-url", summary="返回社区版本地登录地址")
async def sso_authorize_url():
    return success_response(data={"authorize_url": _login_url()})


@router.get("/session/check", summary="检查当前会话状态")
async def session_check(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get(settings.session.cookie_name)
    user_data = await validate_session(token) if token else None
    if user_data is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": 30003,
                "message": "Session expired",
                "data": {"login_url": _login_url()},
            },
        )
    return success_response(data=_serialize_user(db, user_data), message="Session valid")


@router.post("/desktop/handoff", summary="桌面端签发一次性会话交接票据")
async def desktop_handoff(request: Request):
    token = request.cookies.get(settings.session.cookie_name)
    user_data = await validate_session(token) if token else None
    if user_data is None:
        raise HTTPException(status_code=401, detail="No active session")
    handoff = await desktop_ticket_store.issue_ticket({"session_token": token})
    return success_response(
        data={"handoff_ticket": handoff, "expires_in": desktop_ticket_store.ttl_seconds()},
        message="Handoff ticket issued",
    )


@router.post("/desktop/redeem", summary="桌面端兑换会话交接票据")
async def desktop_redeem(body: DesktopRedeemRequest):
    payload = await desktop_ticket_store.consume_ticket(body.ticket.strip())
    token = (payload or {}).get("session_token")
    user_data = await validate_session(token) if token else None
    if user_data is None:
        raise HTTPException(status_code=401, detail="Invalid or expired handoff ticket")
    return success_response(
        data={
            "token": token,
            "cookie_name": settings.session.cookie_name,
            "expires_at": expires_at_iso(user_data.get("ttl_seconds")),
        },
        message="Token redeemed",
    )


@router.post("/logout", summary="登出")
async def logout(request: Request, response: Response):
    token = request.cookies.get(settings.session.cookie_name)
    if token:
        await revoke_session(token)
    _clear_session_cookie(response)
    logger.info("user_logged_out", mode="ce")
    return success_response(data={"login_url": _login_url(), "logout_url": None})
