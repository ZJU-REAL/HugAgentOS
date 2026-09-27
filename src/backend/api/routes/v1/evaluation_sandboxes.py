"""Privileged, owner-bound control plane for isolated evaluation attempts."""
import hmac
import os
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from core.auth.backend import UserContext, get_current_user
from core.config.settings import settings
from core.infra.responses import success_response


async def evaluation_owner(
    user: UserContext = Depends(get_current_user),
    control: Annotated[str | None, Header(alias="X-Evaluation-Control")] = None,
) -> str:
    expected = os.getenv("CONFIG_TOKEN") or settings.auth.config_token
    if not expected or not control or not hmac.compare_digest(control, expected):
        raise HTTPException(403, detail="evaluation_control_required")
    if user.api_key_agent_id:
        raise HTTPException(403, detail="agent_scoped_key_cannot_manage_evaluations")
    return user.user_id


class CreateSandbox(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image: str = Field(pattern=r"^ageval-[A-Za-z0-9._/:@-]+$", max_length=256)
    attempt_id: str = Field(default="", max_length=128)
    ttl_seconds: int = Field(default=7200, ge=60, le=14400)
    cpu_count: int = Field(default=2, ge=1, le=16)
    memory_mb: int = Field(default=4096, ge=512, le=32768)


class ExecCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    argv: list[str] = Field(min_length=1, max_length=128)
    cwd: str = "/attempt/workspace"
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=60, gt=0, le=3600)
    user: str | None = None


router = APIRouter(prefix="/v1/evaluation/sandboxes", tags=["Evaluation"])


async def _call(name: str, *args, **kwargs):
    from core.services import evaluation_sandbox_service as service
    from core.sandbox.errors import SandboxError
    try:
        return await getattr(service, name)(*args, **kwargs)
    except (SandboxError, PermissionError, LookupError) as exc:
        raise HTTPException(409, detail="evaluation_environment_unavailable") from exc


@router.get("")
async def capabilities(owner: str = Depends(evaluation_owner)):
    del owner
    if settings.sandbox.provider != "opensandbox":
        raise HTTPException(409, detail="opensandbox_required")
    return success_response(data={"environment": "hugagent-sandbox", "version": 1})


@router.post("")
async def create(body: CreateSandbox, owner: str = Depends(evaluation_owner)):
    return success_response(data=await _call("create", owner, **body.model_dump()))


@router.post("/{lease_id}/exec")
async def execute(lease_id: str, body: ExecCommand, owner: str = Depends(evaluation_owner)):
    return success_response(data=await _call("execute", lease_id, owner, **body.model_dump()))


@router.put("/{lease_id}/files")
async def upload(lease_id: str, path: str, request: Request, owner: str = Depends(evaluation_owner)):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 64 * 1024 * 1024:
            raise HTTPException(413, detail="evaluation_transfer_too_large")
        chunks.append(chunk)
    await _call("upload", lease_id, owner, path, b"".join(chunks))
    return success_response(data={"bytes": size})


@router.get("/{lease_id}/files")
async def download(lease_id: str, path: str, owner: str = Depends(evaluation_owner)):
    content = await _call("download", lease_id, owner, path)
    return Response(content, media_type="application/octet-stream")


@router.post("/{lease_id}/freeze")
async def freeze(lease_id: str, owner: str = Depends(evaluation_owner)):
    return success_response(data=await _call("freeze", lease_id, owner))


@router.delete("/{lease_id}")
async def destroy(lease_id: str, owner: str = Depends(evaluation_owner)):
    return success_response(data=await _call("destroy", lease_id, owner))
