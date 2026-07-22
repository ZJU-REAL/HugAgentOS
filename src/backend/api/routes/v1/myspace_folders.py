"""My Space · personal folders API.

All routes operate on the current logged-in user's personal space and take the user via
Depends(get_current_user); accepting user_id from path/body is forbidden.

Endpoints:
  GET    /v1/myspace/folders                  → tree or flat
  GET    /v1/myspace/folders/breadcrumb       → breadcrumb
  POST   /v1/myspace/folders                  → create
  PATCH  /v1/myspace/folders/{folder_id}      → rename / move
  DELETE /v1/myspace/folders/{folder_id}      → cascade soft-delete
  GET    /v1/myspace/folders/{folder_id}/affected-count
  POST   /v1/myspace/folders/move-artifact    → move a personal file into a folder (folder_id=None means root)
  POST   /v1/myspace/folders/copy-artifact    → copy a personal file into a folder (keep original, create a copy)
"""

from __future__ import annotations

from typing import Optional

from core.auth.backend import UserContext, get_current_user
from core.db.engine import get_db
from core.infra.responses import success_response
from core.services.user_folder_service import UserFolderService
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

router = APIRouter(prefix="/v1/myspace/folders", tags=["MySpace Folders"])


class CreateFolderBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    parent_folder_id: Optional[str] = None


class UpdateFolderBody(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    parent_folder_id: Optional[str] = None  # null means move to root


class MoveArtifactBody(BaseModel):
    artifact_id: str
    folder_id: Optional[str] = None  # null = root directory


@router.get("", summary="我的文件夹列表")
async def list_folders(
    as_: Optional[str] = Query("tree", alias="as", description="tree | flat"),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出当前用户个人空间的文件夹，as=tree 返回树结构、as=flat 返回扁平列表。"""
    service = UserFolderService(db)
    user_id = str(user.user_id)
    if as_ == "flat":
        folders = service.list_by_user(user_id)
        return success_response(
            data={
                "items": [
                    {
                        "folder_id": f.folder_id,
                        "user_id": f.user_id,
                        "parent_folder_id": f.parent_folder_id,
                        "name": f.name,
                        "created_at": f.created_at.isoformat() if f.created_at else None,
                    }
                    for f in folders
                ]
            }
        )
    return success_response(data={"tree": service.get_tree(user_id)})


@router.get("/breadcrumb", summary="文件夹面包屑路径")
async def get_breadcrumb(
    folder_id: str = Query(...),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """返回指定个人文件夹从根到该节点的面包屑路径。仅限本人文件夹。"""
    service = UserFolderService(db)
    user_id = str(user.user_id)
    folder = service.get_owned(folder_id, user_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="文件夹不存在")
    return success_response(data={"breadcrumb": service.get_breadcrumb(folder_id, user_id)})


@router.post("", summary="创建个人文件夹")
async def create_folder(
    body: CreateFolderBody,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """在当前用户个人空间创建文件夹，可指定父文件夹。"""
    user_id = str(user.user_id)
    result = UserFolderService(db).create_folder(
        user_id=user_id,
        parent_folder_id=body.parent_folder_id,
        name=body.name,
        actor=user_id,
    )
    if not result.ok:
        raise HTTPException(status_code=400, detail=result.message)
    return success_response(data={"folder_id": result.folder_id}, message=result.message)


@router.patch("/{folder_id}", summary="重命名/移动个人文件夹")
async def update_folder(
    folder_id: str,
    body: UpdateFolderBody,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """重命名个人文件夹或移动到新父文件夹（parent_folder_id=null 表示移到根）。仅限本人文件夹。"""
    user_id = str(user.user_id)
    service = UserFolderService(db)
    folder = service.get_owned(folder_id, user_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="文件夹不存在")

    if body.name is not None:
        res = service.rename_folder(folder_id, body.name, actor=user_id)
        if not res.ok:
            raise HTTPException(status_code=400, detail=res.message)

    if "parent_folder_id" in body.model_fields_set:
        res = service.move_folder(folder_id, body.parent_folder_id, actor=user_id)
        if not res.ok:
            raise HTTPException(status_code=400, detail=res.message)

    return success_response(data={"folder_id": folder_id}, message="已更新")


@router.delete("/{folder_id}", summary="删除个人文件夹（级联）")
async def delete_folder(
    folder_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """级联软删除个人文件夹及其内含文件，返回受影响文件数。仅限本人文件夹。"""
    user_id = str(user.user_id)
    service = UserFolderService(db)
    folder = service.get_owned(folder_id, user_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="文件夹不存在")
    result, affected = service.delete_folder(folder_id, actor=user_id)
    if not result.ok:
        raise HTTPException(status_code=400, detail=result.message)
    return success_response(
        data={"folder_id": folder_id, "artifacts_affected": affected},
        message="文件夹及其内容已删除",
    )


@router.get("/{folder_id}/affected-count", summary="文件夹删除影响数预检")
async def affected_count(
    folder_id: str,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """预检文件夹级联删除会影响多少文件（给前端 Modal 提示用）。"""
    user_id = str(user.user_id)
    service = UserFolderService(db)
    folder = service.get_owned(folder_id, user_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="文件夹不存在")
    return success_response(data={"count": service.count_affected_artifacts(folder_id, user_id)})


@router.post("/move-artifact", summary="移动个人文件到文件夹")
async def move_artifact_to_folder(
    body: MoveArtifactBody,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把个人 artifact 移动到指定个人文件夹（folder_id=null → 根目录）。"""
    user_id = str(user.user_id)
    service = UserFolderService(db)
    result = service.move_artifact(body.artifact_id, body.folder_id, actor=user_id)
    if not result.ok:
        # Distinguishing 401/404/400 adds little value; use a uniform 400 so the frontend toast passes through message
        raise HTTPException(status_code=400, detail=result.message)
    return success_response(
        data={"artifact_id": body.artifact_id, "folder_id": body.folder_id}, message=result.message
    )


@router.post("/copy-artifact", summary="复制个人文件到文件夹")
async def copy_artifact_to_folder(
    body: MoveArtifactBody,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """把个人 artifact **复制**到指定个人文件夹（folder_id=null → 根目录），保留原件。"""
    user_id = str(user.user_id)
    service = UserFolderService(db)
    result = service.copy_artifact(body.artifact_id, body.folder_id, actor=user_id)
    if not result.ok:
        raise HTTPException(status_code=400, detail=result.message)
    return success_response(
        data={"artifact_id": result.artifact_id, "folder_id": body.folder_id},
        message=result.message,
    )
