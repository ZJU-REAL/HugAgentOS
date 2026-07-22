"""Community single-owner authorization contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core.db.engine import get_db
from core.db.models import ChatSession, Project
from fastapi import Depends, HTTPException, Path, Request
from sqlalchemy.orm import Session

PermissionLevel = Literal["none", "view", "edit", "admin"]

_RANK = {"none": 0, "view": 1, "edit": 2, "admin": 3}


def has_permission(current: PermissionLevel, required: PermissionLevel) -> bool:
    return _RANK[current] >= _RANK[required]


def resolve_artifact_access(db: Session, user_id: str, owner_id, scope_id) -> PermissionLevel:
    """Resolve access solely from personal ownership."""
    if owner_id and str(owner_id) == str(user_id):
        return "admin"
    return "none"


# ── 项目权限（CE：个人项目 owner 判定保留） ───────────────────────────────────

ProjectPermissionLevel = Literal["none", "view", "edit", "admin"]


def resolve_project_permission(
    db: Session, user_id: str, project: Project
) -> ProjectPermissionLevel:
    if project is None or project.deleted_at is not None:
        return "none"
    if project.kind == "personal":
        return "admin" if project.owner_user_id == user_id else "none"
    # Non-personal legacy data is never visible in CE.
    return "none"


@dataclass
class ProjectAccess:
    """传给路由的访问上下文：project + 用户权限 + user_id。"""

    project: Project
    level: ProjectPermissionLevel
    user_id: str


def require_project_access(min_level: ProjectPermissionLevel = "view"):
    """FastAPI dependency factory（CE 单租户版，保留存在性 404）。"""

    from core.auth.backend import UserContext, get_current_user  # 避免循环导入

    async def _dep(
        project_id: str = Path(..., description="项目 ID"),
        request: Request = None,
        user: "UserContext" = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> ProjectAccess:
        user_id = str(user.user_id)
        project = (
            db.query(Project)
            .filter(Project.project_id == project_id, Project.deleted_at.is_(None))
            .first()
        )
        if project is None:
            raise HTTPException(status_code=404, detail="项目不存在或你无权访问")

        level = resolve_project_permission(db, user_id, project)
        if level == "none":
            raise HTTPException(status_code=404, detail="项目不存在或你无权访问")
        if not has_permission(level, min_level):
            raise HTTPException(status_code=403, detail="当前权限不足")
        return ProjectAccess(project=project, level=level, user_id=user_id)

    return _dep


# ── 会话权限（CE：owner-only） ───────────────────────────────────────────────

ChatAccessLevel = Literal["none", "read", "edit", "admin"]


def resolve_chat_access(db: Session, user_id: str, session: ChatSession) -> ChatAccessLevel:
    if session is None or session.deleted_at is not None:
        return "none"
    return "admin" if session.user_id == user_id else "none"


def can_delete_session(db: Session, user_id: str, session: ChatSession) -> bool:
    if session is None or session.deleted_at is not None:
        return False
    return session.user_id == user_id


__all__ = [
    "ChatAccessLevel",
    "can_delete_session",
    "resolve_chat_access",
    "ProjectAccess",
    "ProjectPermissionLevel",
    "require_project_access",
    "resolve_project_permission",
    "PermissionLevel",
    "has_permission",
    "resolve_artifact_access",
]
