"""Shared CE/EE browser-confirmed desktop login; no custom protocol required."""
import math
import secrets
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from core.auth import desktop_login_store as store
from core.auth.session import validate_session
from core.config.settings import settings
from core.infra.responses import success_response

router = APIRouter(prefix="/v1/auth/desktop/requests", tags=["Auth"])


class DeviceProof(BaseModel):
    device_secret: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")


class BrowserDecision(BaseModel):
    confirm_code: str = Field(min_length=8, max_length=8)
    account_id: str = Field(min_length=1, max_length=256)


def no_store(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


def browser_origin(request: Request):
    # Host is preserved by the deployment proxy. Forwarded headers are not used
    # as independent trust inputs; only the browser's same-origin claim/Host pair.
    origin = request.headers.get("origin", "")
    parsed = urlsplit(origin)
    if (parsed.scheme not in ("http", "https") or parsed.netloc != request.headers.get("host")
            or parsed.path or parsed.query or parsed.fragment
            or request.headers.get("sec-fetch-site") == "cross-site"):
        raise HTTPException(403, "Cross-origin approval rejected")


async def browser_session(request: Request):
    token = request.cookies.get(settings.session.cookie_name, "")
    user = await validate_session(token) if token else None
    if not user:
        raise HTTPException(401, "Please sign in before confirming desktop login")
    return token, user


async def record_for(request_id: str, proof: DeviceProof | None = None):
    record = await store.read(request_id)
    if not record:
        raise HTTPException(410, "Desktop login expired; start again in the desktop app")
    if proof and not secrets.compare_digest(record["secret_hash"], store.digest(proof.device_secret)):
        raise HTTPException(404, "Unknown desktop login")
    return record


def public_status(record: dict):
    return {"status": record["status"], "confirm_code": record["confirm_code"],
            "expires_in": max(0, math.ceil(record["expires_at"] - time.time())),
            "interval": store.POLL_INTERVAL}


@router.post("")
async def start(body: DeviceProof, request: Request, response: Response):
    no_store(response)
    if not await store.allow_create(request.client.host if request.client else "unknown", body.device_secret):
        raise HTTPException(429, "Too many login attempts; try again in one minute",
                            headers={"Retry-After": "60"})
    return success_response(data=await store.create(body.device_secret))


@router.get("/{request_id}")
async def status(request_id: str, request: Request, response: Response):
    no_store(response)
    _, user = await browser_session(request)
    record = await record_for(request_id)
    # After a decision, another signed-in account must not inspect or overwrite it.
    if record.get("user_id") and record["user_id"] != user["user_id"]:
        raise HTTPException(403, "This login was confirmed by another account")
    return success_response(data={**public_status(record), "username": user.get("username", ""),
                                  "email": user.get("email"), "account_id": str(user["user_id"])})


async def decide(request_id: str, body: BrowserDecision, request: Request, decision: str):
    browser_origin(request)
    token, user = await browser_session(request)
    if body.account_id != str(user["user_id"]):
        raise HTTPException(409, "Account changed; refresh and confirm again")
    for _ in range(8):
        record = await record_for(request_id)
        if not secrets.compare_digest(record["confirm_code"], body.confirm_code):
            raise HTTPException(400, "Confirmation code does not match")
        if record["status"] != "pending":
            if record.get("user_id") == user["user_id"] and record["status"] in (
                decision, "delivered", "completed"
            ):
                return public_status(record)
            raise HTTPException(409, "Login request is no longer pending")
        updated = {**record, "status": decision, "user_id": user["user_id"]}
        if decision == "approved":
            updated["token"] = token
        if await store.compare_set(request_id, record, updated):
            return public_status(updated)
    raise HTTPException(409, "Concurrent update; retry")


@router.post("/{request_id}/approve")
async def approve(request_id: str, body: BrowserDecision, request: Request, response: Response):
    no_store(response)
    return success_response(data=await decide(request_id, body, request, "approved"))


@router.post("/{request_id}/deny")
async def deny(request_id: str, body: BrowserDecision, request: Request, response: Response):
    no_store(response)
    return success_response(data=await decide(request_id, body, request, "denied"))


async def device_action(request_id: str, body: DeviceProof, action: str):
    for _ in range(8):
        record = await record_for(request_id, body)
        updated = dict(record)
        status = record["status"]
        if action == "cancel" and status not in ("completed", "cancelled", "denied"):
            updated["status"] = "cancelled"
            updated.pop("token", None)
        elif action == "ack" and status == "delivered":
            updated["status"] = "completed"
            updated.pop("token", None)
        elif action == "poll" and status in ("approved", "delivered"):
            # Do not deliver a session revoked since browser approval.
            if not await validate_session(record["token"]):
                updated["status"] = "cancelled"
                updated.pop("token", None)
            else:
                updated["status"] = "delivered"
        if (updated != record or (action == "poll" and status == "delivered")) and not await store.compare_set(request_id, record, updated):
            continue
        result = public_status(updated)
        if action == "poll" and updated["status"] == "delivered":
            result["token"] = updated["token"]
            result["cookie_name"] = settings.session.cookie_name
        return result
    raise HTTPException(409, "Concurrent update; retry")


@router.post("/{request_id}/poll")
async def poll(request_id: str, body: DeviceProof, response: Response):
    no_store(response)
    return success_response(data=await device_action(request_id, body, "poll"))


@router.post("/{request_id}/ack")
async def ack(request_id: str, body: DeviceProof, response: Response):
    no_store(response)
    return success_response(data=await device_action(request_id, body, "ack"))


@router.post("/{request_id}/cancel")
async def cancel(request_id: str, body: DeviceProof, response: Response):
    no_store(response)
    return success_response(data=await device_action(request_id, body, "cancel"))
