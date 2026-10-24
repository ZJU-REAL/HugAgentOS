"""Session manager routes and separate device consent mailbox."""

from fastapi import APIRouter, Depends, Request, HTTPException, Header
from pydantic import BaseModel, Field, ConfigDict
from core.db.engine import get_db
from core.db.threads import run_in_session
from core.infra.responses import success_response
from personal_agent import access
from personal_agent.sessions.session_auth import resolve
from personal_agent.sessions import session_queue
from personal_agent.sessions.session_manager import dispatch
from personal_agent.computer.devices import list_devices

router = APIRouter(prefix="/v1/personal-agent", tags=["personal-agent"])


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = Field(default="cloud", max_length=100)
    operation: str = Field(max_length=40)
    args: dict = Field(default_factory=dict)
    request_key: str = Field(min_length=1, max_length=100)


async def identity(request: Request):
    user, grant = await resolve(request)
    await run_in_session(access.require, user.user_id)
    return user, grant


def perform(fn):
    try:
        return success_response(data=fn())
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/session-manager/devices")
def devices(auth=Depends(identity)):
    user, _ = auth
    return success_response(data=list_devices(user.user_id))


@router.post("/session-manager/operations", status_code=202)
async def submit(body: OperationRequest, auth=Depends(identity)):
    user, grant = auth
    try:
        return success_response(
            data=await dispatch(
                user, body.target, body.operation, body.args, body.request_key, grant
            )
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/session-manager/operations/{ident}")
def get_operation(ident: str, auth=Depends(identity), db=Depends(get_db)):
    user, grant = auth
    return perform(
        lambda: session_queue.view(db, user.user_id, ident, grant["task"] if grant else None)
    )


def token(authorization: str = Header()):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Device credential required")
    return authorization[7:]


class Consent(BaseModel):
    enabled: bool


@router.post("/device-link/{ident}/sessions/consent")
def consent(ident: str, body: Consent, secret=Depends(token), db=Depends(get_db)):
    return perform(lambda: session_queue.consent(db, ident, secret, body.enabled))


@router.get("/device-link/{ident}/sessions/operations")
def poll(ident: str, secret=Depends(token), db=Depends(get_db)):
    return perform(lambda: session_queue.poll(db, ident, secret))


class Receipt(BaseModel):
    result: dict | list | str | int | bool | None


@router.post("/device-link/{ident}/sessions/operations/{operation_id}")
def acknowledge(
    ident: str, operation_id: str, body: Receipt, secret=Depends(token), db=Depends(get_db)
):
    return perform(lambda: session_queue.acknowledge(db, ident, secret, operation_id, body.result))


@router.get("/device-link/{ident}/sessions/operations/{operation_id}/check")
def check(ident: str, operation_id: str, secret=Depends(token), db=Depends(get_db)):
    return perform(lambda: session_queue.check(db, ident, secret, operation_id))
