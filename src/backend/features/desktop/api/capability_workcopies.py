"""capability workcopies responsibilities."""
from __future__ import annotations
from fastapi import APIRouter
from features.desktop.gateway.observability import ObservedGatewayRoute
router = APIRouter(prefix="/v1/desktop/capability", tags=["Desktop Capability"], route_class=ObservedGatewayRoute)
from core.infra.responses import success_response
from fastapi import Depends, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from features.desktop.api import capability_dependencies
from features.desktop.api import desktop_capability_state

class WorkcopyCommitBody(BaseModel):
    files: dict[str, str]
    expected_revision: str | None = None
    expected_model_version: str | None = None
    request_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    acknowledge_sensitive: bool = False
    create_only: bool = False


@router.get("/workcopies/{kind}/{key}", summary="比较当前账号的能力文件版本")
async def workcopy_snapshot(kind: str, key: str, response: Response,
                            user_id: str = Depends(capability_dependencies.require_capability_user)):
    from features.desktop.capability.workcopies import snapshot
    response.headers["Cache-Control"] = "no-store"
    result = await run_in_threadpool(snapshot, user_id, kind, key)
    return success_response(data=await run_in_threadpool(capability_dependencies.public_content, user_id, result))


@router.post("/workcopies/{kind}/{key}", summary="显式提交能力文件版本")
async def workcopy_commit(kind: str, key: str, body: WorkcopyCommitBody, response: Response,
                          user_id: str = Depends(capability_dependencies.require_capability_user)):
    from features.desktop.capability.workcopies import commit
    response.headers["Cache-Control"] = "no-store"
    await run_in_threadpool(capability_dependencies.public_content, user_id, body.files)
    result = await run_in_threadpool(commit, user_id, kind, key, body.model_dump())
    return success_response(data=await run_in_threadpool(capability_dependencies.public_content, user_id, result))
