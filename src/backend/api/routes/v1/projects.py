"""Projects (Claude-style 工作空间) API — CE 子集（split：§5.2）.

社区版为单租户，只提供与个人文件夹绑定的个人项目。

端点：

    GET    /v1/projects                      列表（个人）
    POST   /v1/projects                      创建（仅 kind=personal）
    GET    /v1/projects/{id}                 详情（带 folder_name / 容量）
    PATCH  /v1/projects/{id}                 改名 / 描述 / pin / icon / instructions
    DELETE /v1/projects/{id}                 软删（不删挂钩文件夹）
    POST   /v1/projects/{id}/favorite        star
    DELETE /v1/projects/{id}/favorite        取消 star
    GET    /v1/projects/{id}/files           项目文件列表（递归挂钩文件夹子树）
    POST   /v1/projects/{id}/files/upload    直传（``filename`` 可含 path 自动 mkdir 子文件夹）
    DELETE /v1/projects/{id}/files/{artifact_id}  从项目 / MySpace 中软删该文件
    PATCH  /v1/projects/{id}/instructions    更新项目指令
    GET    /v1/projects/{id}/chats           当前用户在本项目内的会话列表
"""

from __future__ import annotations

import mimetypes
import os
from typing import Literal, Optional
from urllib.parse import quote

from core.auth.backend import UserContext, get_current_user
from core.auth.permissions_iface import ProjectAccess, require_project_access
from core.config.local_mode import local_mode_enabled
from core.db.engine import get_db
from core.db.models import Artifact
from core.infra.responses import created_response, paginated_response, success_response
from core.services.project_file_service import ProjectFileService
from core.services.project_service import ProjectService
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

router = APIRouter(prefix="/v1/projects", tags=["projects"])


# ── Request / Response models ────────────────────────────────────────────


class CreateProjectBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    kind: Literal["personal", "local"] = "personal"
    # 可选：复用已有文件夹挂钩；不传则后端在根目录新建同名文件夹
    linked_folder_id: Optional[str] = None
    # kind=local（桌面本地模式）：绑定的本机真实文件夹绝对路径 + 机器标识
    local_path: Optional[str] = None
    local_machine: Optional[str] = None


class UpdateProjectBody(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    instructions: Optional[str] = Field(None, max_length=8000)
    pinned: Optional[bool] = None
    icon_color: Optional[str] = Field(None, max_length=20)
    # 项目级记忆开关（读 / 写）—— 进入项目后完全覆盖用户级 memory_enabled / memory_write_enabled。
    memory_enabled: Optional[bool] = None
    memory_write_enabled: Optional[bool] = None


class UpdateInstructionsBody(BaseModel):
    instructions: str = Field("", max_length=8000)


# ── List / Create ────────────────────────────────────────────────────────


@router.get("", summary="列出可见项目")
async def list_projects(
    q: Optional[str] = Query(None, description="按 name / description 模糊搜索"),
    sort: str = Query("-last_activity_at", description="-last_activity_at | name | created"),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """分页列出当前用户可见的项目，支持关键词搜索与排序。"""
    svc = ProjectService(db)
    items, total = svc.list_visible(
        str(user.user_id), q=q, sort=sort, page=page, page_size=page_size
    )
    return paginated_response(items=items, page=page, page_size=page_size, total_items=total)


@router.post("", status_code=status.HTTP_201_CREATED, summary="创建项目")
async def create_project(
    body: CreateProjectBody,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """创建个人项目（云端）或本地文件夹项目（桌面本地模式）。

    kind=local 只在桌面本地部署可用，由本地部署门控保证云端不会创建本地项目。
    """
    svc = ProjectService(db)
    user_id = str(user.user_id)
    if body.kind == "local":
        if not local_mode_enabled():
            raise HTTPException(status_code=403, detail="本地项目仅在桌面本地模式下可用")
        if not body.local_path:
            raise HTTPException(status_code=400, detail="本地项目必须指定文件夹路径")
        project = svc.create_local(
            user_id,
            body.name,
            body.local_path,
            description=body.description,
            machine=body.local_machine,
        )
    else:
        project = svc.create_personal(
            user_id,
            body.name,
            body.description,
            linked_folder_id=body.linked_folder_id,
        )
    return created_response(data=svc.get(project.project_id, user_id))


# ── Detail / Update / Delete ─────────────────────────────────────────────


@router.get("/{project_id}", summary="项目详情")
async def get_project(
    access: ProjectAccess = Depends(require_project_access("view")),
    db: Session = Depends(get_db),
):
    """获取项目详情，附带挂钩文件夹名称及容量（已用 / 上限）；项目不存在返回 404。"""
    svc = ProjectService(db)
    data = svc.get(access.project.project_id, access.user_id)
    if data is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    pf_svc = ProjectFileService(db)
    data["capacity_used"] = pf_svc.capacity_used(access.project)
    data["capacity_limit"] = pf_svc.capacity_limit()
    return success_response(data=data)


@router.patch("/{project_id}", summary="更新项目元信息 / instructions")
async def update_project(
    body: UpdateProjectBody,
    access: ProjectAccess = Depends(require_project_access("edit")),
    db: Session = Depends(get_db),
):
    """更新项目元信息（名称 / 描述 / pin / 图标 / instructions / 项目级记忆开关）；需 edit 权限。"""
    svc = ProjectService(db)
    patch = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    data = svc.update(access.project.project_id, access.user_id, patch, level=access.level)
    if data is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return success_response(data=data, message="已更新")


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT, summary="软删项目")
async def delete_project(
    access: ProjectAccess = Depends(require_project_access("admin")),
    db: Session = Depends(get_db),
):
    """软删项目，不删除挂钩文件夹及其文件。"""
    svc = ProjectService(db)
    svc.soft_delete(access.project.project_id, access.user_id)
    return success_response(message="已删除")


# ── Favorite ──────────────────────────────────────────────────────────────


@router.post("/{project_id}/favorite", summary="收藏项目")
async def favorite_project(
    access: ProjectAccess = Depends(require_project_access("view")),
    db: Session = Depends(get_db),
):
    """收藏（star）当前项目。"""
    svc = ProjectService(db)
    svc.toggle_favorite(access.project.project_id, access.user_id, on=True)
    return success_response(data={"favorite": True})


@router.delete("/{project_id}/favorite", summary="取消收藏")
async def unfavorite_project(
    access: ProjectAccess = Depends(require_project_access("view")),
    db: Session = Depends(get_db),
):
    """取消收藏当前项目。"""
    svc = ProjectService(db)
    svc.toggle_favorite(access.project.project_id, access.user_id, on=False)
    return success_response(data={"favorite": False})


# ── Instructions ──────────────────────────────────────────────────────────


@router.patch("/{project_id}/instructions", summary="更新项目指令（system prompt 末段注入）")
async def update_instructions(
    body: UpdateInstructionsBody,
    access: ProjectAccess = Depends(require_project_access("edit")),
    db: Session = Depends(get_db),
):
    """更新项目指令（instructions），会注入到 system prompt 末段；需 edit 权限。"""
    svc = ProjectService(db)
    data = svc.update(
        access.project.project_id,
        access.user_id,
        {"instructions": body.instructions},
        level=access.level,
    )
    if data is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return success_response(data=data, message="项目指令已更新")


# ── Files（基于挂钩文件夹子树） ──────────────────────────────────────────


@router.get("/{project_id}/files", summary="项目文件列表（递归挂钩文件夹子树）")
async def list_project_files(
    access: ProjectAccess = Depends(require_project_access("view")),
    db: Session = Depends(get_db),
):
    """列出项目文件。本地项目遍历本机真实文件夹；云端项目遍历挂钩文件夹子树。"""
    if access.project.kind == "local":
        from core.sandbox.local_mount import list_local_files

        local = (access.project.extra_data or {}).get("local") or {}
        raw = list_local_files(local.get("path") or "")
        pid = access.project.project_id
        # 对齐云端项目文件的条目形态（ProjectFileItem），download_url 指向本地文件
        # 原始内容端点，前端预览/下载直接可用；artifact_id 复用相对路径（本地文件
        # 没有 artifact，删除入口在前端对本地项目隐藏）。
        items = []
        for f in raw:
            rel = f["path"]
            mime = mimetypes.guess_type(f["name"])[0] or "application/octet-stream"
            items.append(
                {
                    "id": rel,
                    "artifact_id": rel,
                    "name": rel,
                    "title": f["name"],
                    "mime_type": mime,
                    "size_bytes": f["size"],
                    "download_url": f"/v1/projects/{pid}/local-files/raw?path={quote(rel)}",
                    "type": "image" if mime.startswith("image/") else "document",
                    "folder_path": rel.rsplit("/", 1)[0] if "/" in rel else "",
                    "created_at": f.get("mtime"),
                }
            )
        return success_response(
            data={"items": items, "total": len(items), "local_path": local.get("path")}
        )
    pf_svc = ProjectFileService(db)
    items = pf_svc.list_files(access.project)
    return success_response(
        data={
            "items": items,
            "total": len(items),
            "capacity_used": pf_svc.capacity_used(access.project),
            "capacity_limit": pf_svc.capacity_limit(),
        }
    )


@router.get("/{project_id}/local-files/raw", summary="读取本地项目文件原始内容（预览 / 下载）")
async def get_local_project_file(
    path: str = Query(..., description="项目根目录下的相对路径（POSIX 分隔符）"),
    access: ProjectAccess = Depends(require_project_access("view")),
):
    """流式返回本地项目文件夹内某个文件的原始内容（inline，供前端预览与下载）。

    仅本地项目 + 桌面本地模式可用；路径经 realpath 归一后必须仍落在项目根目录内，
    杜绝 ``..`` / 符号链接越界读取。
    """
    if access.project.kind != "local" or not local_mode_enabled():
        raise HTTPException(status_code=404, detail="文件不存在")
    local = (access.project.extra_data or {}).get("local") or {}
    root = os.path.realpath(os.path.expanduser((local.get("path") or "").strip()))
    if not root or not os.path.isdir(root):
        raise HTTPException(status_code=404, detail="本地项目文件夹不存在或不可访问")
    full = os.path.realpath(os.path.join(root, path))
    if full != root and not full.startswith(root + os.sep):
        raise HTTPException(status_code=403, detail="路径越界")
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="文件不存在")
    mime = mimetypes.guess_type(full)[0] or "application/octet-stream"
    return FileResponse(full, media_type=mime)


@router.post("/{project_id}/files/upload", summary="直传文件到项目（写入挂钩文件夹）")
async def upload_project_file(
    file: UploadFile = File(...),
    access: ProjectAccess = Depends(require_project_access("edit")),
    db: Session = Depends(get_db),
):
    """直传文件到项目挂钩文件夹（filename 含路径时自动创建子文件夹）；需 edit 权限。"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空")
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="文件内容为空")
    pf_svc = ProjectFileService(db)
    data = pf_svc.upload(
        access.project,
        access.user_id,
        contents,
        file.filename,
        file.content_type,
    )
    ProjectService(db).touch_activity(access.project.project_id)
    return created_response(data=data, message="上传成功")


@router.delete("/{project_id}/files/{artifact_id}", summary="软删项目文件（同步 MySpace）")
async def remove_project_file(
    artifact_id: str,
    access: ProjectAccess = Depends(require_project_access("edit")),
    db: Session = Depends(get_db),
):
    """直接软删 artifact —— 因为项目文件即 MySpace 文件夹下的文件，
    项目里删 = MySpace 里也删。"""
    from datetime import datetime

    # 校验 artifact 属于本项目挂钩文件夹的子树（CE 只有个人项目）
    art = (
        db.query(Artifact)
        .filter(Artifact.artifact_id == artifact_id, Artifact.deleted_at.is_(None))
        .first()
    )
    if art is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    pf_svc = ProjectFileService(db)
    project = access.project
    if project.kind != "personal":
        raise HTTPException(status_code=404, detail="项目不存在")
    subtree = pf_svc._user_subtree_ids(project.owner_user_id, project.linked_folder_id or "")
    if not subtree or art.user_id != project.owner_user_id or art.user_folder_id not in subtree:
        raise HTTPException(status_code=404, detail="文件不在本项目挂钩文件夹内")

    art.deleted_at = datetime.utcnow()
    db.commit()
    return success_response(message="已删除")


# ── Chats within a project ───────────────────────────────────────────────


@router.get("/{project_id}/chats", summary="项目内的会话列表")
async def list_project_chats(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    scope: Literal["all", "mine", "shared"] = Query(
        "all", description="all=自己+共享给我的；mine=仅我创建；shared=仅他人共享给我的"
    ),
    access: ProjectAccess = Depends(require_project_access("view")),
    db: Session = Depends(get_db),
):
    """个人项目下退化为只列出当前用户自己的会话。"""
    svc = ProjectService(db)
    items, total = svc.list_chats(
        access.project.project_id,
        access.user_id,
        page=page,
        page_size=page_size,
        scope=scope,
    )
    return paginated_response(items=items, page=page, page_size=page_size, total_items=total)
