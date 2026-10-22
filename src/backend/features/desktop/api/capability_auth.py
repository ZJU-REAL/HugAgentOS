"""capability auth responsibilities."""
from __future__ import annotations
from fastapi import APIRouter
from features.desktop.gateway.observability import ObservedGatewayRoute
router = APIRouter(prefix="/v1/desktop/capability", tags=["Desktop Capability"], route_class=ObservedGatewayRoute)
from core.infra.responses import success_response
from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, Field
from features.desktop.api import desktop_capability_state

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
    from features.desktop.capability.service import issue_capability_token, session_authorization_epoch

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
