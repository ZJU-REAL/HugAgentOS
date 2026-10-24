"""This computer's Personal Agent link (desktop execution plane only).

The desktop UI pairs the device with the cloud, then hands the credential to this
backend, which keeps it privately and serves the device mailbox. Consent to screen
control and to session management is set here, on this computer, and only here.
Every route answers 404 outside the backend the desktop shell started.
"""

import platform
from typing import Optional

from core.auth.backend import UserContext, get_current_user
from features.desktop.auth.bridge import bridge_enabled
from personal_agent.device_link import ACTIONS, SESSIONS, store
from personal_agent.device_link.runner import current_account, current_link, run_driver, runner
from core.infra.responses import success_response
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/v1/device-link", tags=["Device Link"])

PLATFORMS = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}


def linked_account(user: UserContext = Depends(get_current_user)) -> tuple:
    if not bridge_enabled():
        raise HTTPException(404, "Not Found")
    owner = current_account()
    if not owner or owner[0] != user.user_center_id:
        raise HTTPException(403, "Only the signed-in desktop account can link this computer")
    return owner


def _view(link: Optional[store.Link]):
    if link is None:
        return None
    return {
        "device_id": link.device_id,
        "name": link.name,
        "control_enabled": link.control_enabled,
        "sessions_enabled": link.sessions_enabled,
    }


async def _status(owner):
    probe = await run_driver({"type": "probe"})
    control = bool(probe.get("control"))
    link = current_link(owner)
    return {
        "platform": PLATFORMS.get(platform.system(), "linux"),
        "default_name": platform.node(),
        "capabilities": sorted((ACTIONS if control else frozenset()) | {SESSIONS}),
        "control_supported": control,
        "permissions": probe.get("permissions"),
        "link": _view(link),
        "state": runner.state if link else "unlinked",
    }


@router.get("", summary="本机连接状态")
async def status(owner=Depends(linked_account)):
    return success_response(data=await _status(owner))


class LinkBody(BaseModel):
    device_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    token: str = Field(min_length=20, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    control_enabled: bool = False
    sessions_enabled: bool = False


async def _require_control(enabled: bool) -> None:
    if enabled and not (await run_driver({"type": "probe"})).get("control"):
        raise HTTPException(409, "Screen control is not supported on this computer")


@router.put("", summary="保存云端配对并连接这台电脑")
async def link(body: LinkBody, owner=Depends(linked_account)):
    await _require_control(body.control_enabled)
    account, cloud_base = owner
    previous = store.load(account)
    if previous and previous.device_id != body.device_id:
        # Receipts of the previous device can no longer be delivered.
        store.delete(account)
    store.save(account, store.Link(cloud_base=cloud_base, **body.model_dump()))
    runner.wake()
    return success_response(data=await _status(owner))


class ConsentBody(BaseModel):
    control_enabled: Optional[bool] = None
    sessions_enabled: Optional[bool] = None


@router.patch("", summary="修改本机许可")
async def consent(body: ConsentBody, owner=Depends(linked_account)):
    link = current_link(owner)
    if link is None:
        raise HTTPException(404, "This computer is not linked")
    if body.control_enabled is not None:
        await _require_control(body.control_enabled)
        link.control_enabled = body.control_enabled
    if body.sessions_enabled is not None:
        link.sessions_enabled = body.sessions_enabled
    store.save(owner[0], link)
    runner.wake()
    return success_response(data=await _status(owner))


@router.delete("", summary="删除这台电脑的连接")
async def unlink(owner=Depends(linked_account)):
    store.delete(owner[0])
    runner.wake()
    return success_response(data=await _status(owner))


@router.post("/permissions", summary="请求系统授权（macOS 屏幕录制与辅助功能）")
async def request_permissions(owner=Depends(linked_account)):
    await run_driver({"type": "request_permissions"})
    return success_response(data=await _status(owner))
