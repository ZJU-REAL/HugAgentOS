"""Desktop channel registration and leased execution; device capabilities only."""

from features.desktop.api.capability_dependencies import require_capability_user as _require_capability_user
from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.db.threads import run_in_session
from core.infra.responses import success_response
from integrations.channels.relay import ChannelRelayService
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

router = APIRouter(prefix="/v1/channels/desktop", tags=["Channels"])


class Register(BaseModel):
    device_name: str = Field(default="本机", max_length=100)


class Poll(BaseModel):
    binding_ids: list[str] = Field(default_factory=list, max_length=200)


class Lease(BaseModel):
    lease: str = Field(min_length=32, max_length=64)
    error: str | None = Field(default=None, max_length=300)


class Operation(Lease):
    operation_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,48}$")
    method: str = Field(max_length=40)
    args: dict = Field(default_factory=dict)


def device(request):
    return request.headers.get("x-desktop-device-id", "")


@router.post("/register")
def register(
    body: Register,
    request: Request,
    owner: str = Depends(_require_capability_user),
    db: Session = Depends(get_db),
):
    return success_response(
        data=ChannelRelayService(db).register(owner, device(request), body.device_name)
    )


@router.post("/claim")
def claim(
    body: Poll,
    request: Request,
    owner: str = Depends(_require_capability_user),
    db: Session = Depends(get_db),
):
    from dataclasses import asdict

    from integrations.channels.registry import get_adapter

    item = ChannelRelayService(db).claim(owner, device(request), body.binding_ids)
    if item:
        item["caps"] = asdict(get_adapter(item["message"]["channel_type"]).caps)
    retained = ChannelRelayService(db).retained_bindings(owner, device(request), body.binding_ids)
    return success_response(data={"delivery": item, "retained_binding_ids": retained})


@router.post("/{delivery_id}/renew")
def renew(
    delivery_id: str,
    body: Lease,
    request: Request,
    owner: str = Depends(_require_capability_user),
    db: Session = Depends(get_db),
):
    ChannelRelayService(db).renew(owner, device(request), delivery_id, body.lease)
    return success_response(data={"ok": True})


def _complete(db, owner, device_id, delivery_id, body):
    svc = ChannelRelayService(db)
    row, conn = svc.authorized(owner, device_id, delivery_id, body.lease)
    payload = row.payload
    db.expunge(conn)
    svc.complete(owner, device_id, delivery_id, body.lease, body.error)
    return payload, conn


@router.post("/{delivery_id}/complete")
async def complete(
    delivery_id: str,
    body: Lease,
    request: Request,
    owner: str = Depends(_require_capability_user),
):
    payload, conn = await run_in_session(_complete, owner, device(request), delivery_id, body)
    if body.error:
        import logging

        from integrations.channels.protocol import InboundMsg
        from integrations.channels.registry import get_adapter

        msg = InboundMsg(**payload["message"])
        if msg.addressed_to_bot is not False:
            try:
                await get_adapter(conn.channel_type).send_text(
                    conn,
                    msg,
                    "本机执行未完成，请检查桌面端记录或权限后重发消息。系统不会自动转云端或重复执行。",
                )
            except Exception:
                logging.getLogger(__name__).warning("desktop channel failure notification failed")
    return success_response(data={"ok": True})


@router.post("/{delivery_id}/operation")
async def operation(
    delivery_id: str,
    body: Operation,
    request: Request,
    owner: str = Depends(_require_capability_user),
):
    from integrations.channels.relay_rpc import channel_operation

    data = await channel_operation(
        owner,
        device(request),
        delivery_id,
        body.lease,
        body.operation_id,
        body.method,
        body.args,
    )
    return success_response(data=data)


@router.post("/local-binding")
async def local_binding(user: UserContext = Depends(get_current_user)):
    from integrations.channels.desktop_worker import prepare_binding

    return success_response(data=await prepare_binding(str(user.user_id)))
