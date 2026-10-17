"""Personal Agent computer routes."""

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket

from sqlalchemy.orm import Session

from core.db.engine import get_db

from core.infra.responses import success_response

from personal_agent.api.schemas import DevicePair
from personal_agent.api.schemas import DeviceReceipt
from personal_agent.api.schemas import LocalConsent
from personal_agent.api.schemas import HumanControl
from personal_agent.api.schemas import StreamTicket
from personal_agent.api.dependencies import service
from personal_agent.api.dependencies import perform
from personal_agent.api.dependencies import device_token

router = APIRouter()


@router.get("/devices")
def devices(svc=Depends(service)):
    from personal_agent.computer.devices import list_devices

    return success_response(data=list_devices(svc.user_id))


@router.post("/devices")
def pair_device(body: DevicePair, svc=Depends(service)):
    from personal_agent.computer.devices import pair

    return perform(lambda: pair(svc.db, svc.user_id, body.name, body.platform, body.capabilities))


@router.delete("/devices/{ident}")
def revoke_device(ident: str, svc=Depends(service)):
    from personal_agent.models import PersonalDevice, PersonalDeviceCommand
    from sqlalchemy import select, update

    row = svc.db.scalar(
        select(PersonalDevice)
        .where(PersonalDevice.id == ident, PersonalDevice.user_id == svc.user_id)
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "Device not found")
    from personal_agent.models import PersonalSessionOperation

    row.revoked, row.epoch = 1, row.epoch + 1
    row.session_epoch += 1
    row.sessions_enabled = 0
    svc.db.execute(
        update(PersonalSessionOperation)
        .where(
            PersonalSessionOperation.device_id == ident,
            PersonalSessionOperation.status.in_(["queued", "sent"]),
        )
        .values(status="revoked")
    )
    svc.db.execute(
        update(PersonalDeviceCommand)
        .where(
            PersonalDeviceCommand.device_id == ident,
            PersonalDeviceCommand.status.in_(["queued", "sent"]),
        )
        .values(status="revoked")
    )
    svc.db.commit()
    return success_response(data={"revoked": True})


@router.get("/device-link/{ident}/commands")
def device_poll(ident: str, token=Depends(device_token), db: Session = Depends(get_db)):
    from personal_agent.computer.devices import poll

    return perform(lambda: poll(db, ident, token))


@router.post("/device-link/{ident}/commands/{command_id}")
def device_receipt(
    ident: str,
    command_id: str,
    body: DeviceReceipt,
    token=Depends(device_token),
    db: Session = Depends(get_db),
):
    from personal_agent.computer.devices import acknowledge

    return perform(lambda: acknowledge(db, ident, token, command_id, body.result))


@router.post("/device-link/{ident}/consent")
def device_consent(
    ident: str, body: LocalConsent, token=Depends(device_token), db: Session = Depends(get_db)
):
    from personal_agent.computer.devices import consent

    return perform(lambda: consent(db, ident, token, body.enabled))


async def control_call(fn):
    try:
        return success_response(data=await fn())
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from None


@router.post("/computer/control")
async def begin_control(svc=Depends(service)):
    from personal_agent.computer.human_control import begin

    return await control_call(lambda: begin(svc))


@router.get("/computer/preview")
async def computer_preview(svc=Depends(service)):
    from personal_agent.computer.human_control import preview

    return await control_call(lambda: preview(svc))


@router.post("/computer/stream")
async def computer_stream_ticket(body: StreamTicket, svc=Depends(service)):
    from personal_agent.computer.live_stream import ticket

    return await control_call(lambda: ticket(svc, body.token))


@router.websocket("/computer/stream/ws")
async def computer_stream(websocket: WebSocket, ticket: str = Query("", max_length=100)):
    # The single-use ticket carries the owner and the control session. No cookie is read here.
    from personal_agent.computer.live_stream import relay

    await relay(websocket, ticket)


@router.delete("/computer/control")
async def end_control(body: HumanControl, svc=Depends(service)):
    from personal_agent.computer.human_control import end

    return await control_call(lambda: end(svc, body.token))
