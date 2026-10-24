"""Desktop local-mode permission API (ticket #06).

Folder grants + danger-command policy for the desktop local backend. Every route
is gated on ``local_mode_enabled()`` (DEPLOY_PROFILE=local) and returns 403
otherwise, so the cloud/web deployment never exposes local-permission control.
"""

from __future__ import annotations

from typing import Literal, Optional

from core.auth.backend import UserContext, get_current_user
from core.config.local_mode import local_mode_enabled
from core.infra.responses import success_response
from features.desktop.local import grants as grants
from features.desktop.local import snapshots as snapshots
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/v1/local", tags=["local"])


def _require_local() -> None:
    if not local_mode_enabled():
        raise HTTPException(status_code=403, detail="本地权限管理仅在桌面本地模式下可用")


class AddGrantBody(BaseModel):
    path: str = Field(..., min_length=1)
    mode: Literal["read", "readwrite"] = "readwrite"


class PolicyBody(BaseModel):
    out_of_scope: Optional[Literal["block", "confirm", "allow"]] = None
    delete: Optional[Literal["block", "confirm", "allow"]] = None
    system_write: Optional[Literal["block", "confirm", "allow"]] = None
    network: Optional[Literal["block", "confirm", "allow"]] = None
    privilege: Optional[Literal["block", "confirm", "allow"]] = None


@router.get("/grants", summary="列出已授权的本地目录")
async def list_grants(_user: UserContext = Depends(get_current_user)):
    _require_local()
    return success_response(data={"items": grants.list_grants()})


@router.post("/grants", summary="授权一个本地目录")
async def add_grant(body: AddGrantBody, _user: UserContext = Depends(get_current_user)):
    _require_local()
    try:
        entry = grants.add_grant(body.path, body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return success_response(data=entry, message="已授权")


@router.delete("/grants", summary="撤销一个本地目录授权")
async def remove_grant(path: str, _user: UserContext = Depends(get_current_user)):
    _require_local()
    grants.remove_grant(path)
    return success_response(data={"path": path}, message="已撤销")


@router.get("/policy", summary="获取危险命令策略")
async def get_policy(_user: UserContext = Depends(get_current_user)):
    _require_local()
    return success_response(data=grants.get_policy())


@router.put("/policy", summary="更新危险命令策略")
async def set_policy(body: PolicyBody, _user: UserContext = Depends(get_current_user)):
    _require_local()
    try:
        saved = grants.set_policy(body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return success_response(data=saved, message="策略已更新")


from features.desktop.edition import register_local_routes
register_local_routes(router, _require_local)


class RollbackBody(BaseModel):
    path: str = Field(..., min_length=1)


@router.get("/snapshots", summary="列出有写回快照的本地文件")
async def list_snapshots(_user: UserContext = Depends(get_current_user)):
    _require_local()
    return success_response(data={"items": snapshots.list_snapshotted_files()})


@router.post("/rollback", summary="把某个本地文件回滚到上一版快照")
async def rollback(body: RollbackBody, _user: UserContext = Depends(get_current_user)):
    _require_local()
    ok = snapshots.rollback(body.path)
    if not ok:
        raise HTTPException(status_code=404, detail="该文件没有可回滚的快照")
    return success_response(data={"path": body.path}, message="已回滚到上一版")
