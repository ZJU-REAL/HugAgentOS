"""权限接口层 —— 社区版单租户 stub。

CE 没有团队/多租户：当前用户对自己的资源恒为最高权限，对他人资源恒不可见。
与商业版的真实现保持同一组符号与签名（接缝 C3），调用方零改动。

退化原则（对齐主仓施工图附录 D）：
  - owner 语义保留：自己的资源恒最高权限（由各调用方的 owner 判定承担）；
  - 团队权限恒 ``none``——CE 无团队概念，team_id 标记的他人资源一律不可见
    （从 EE 迁移来的存量团队数据不能因 stub 放行而对全员可读）；
  - 资源「存在性 404」保留；
  - 不写审计日志（审计属商业版）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Depends, HTTPException, Path, Request
from sqlalchemy.orm import Session

from core.db.engine import get_db
from core.db.models import ChatSession, Project

# ── 团队文件权限（CE：单租户恒 admin） ────────────────────────────────────────

PermissionLevel = Literal["none", "view", "edit", "admin"]

_RANK = {"none": 0, "view": 1, "edit": 2, "admin": 3}


def resolve_team_file_permission(db: Session, user_id: str, team_id: str) -> PermissionLevel:
    # CE 无团队：team_id 标记的资源不经团队通道放行（owner 通道由调用方保留）。
    # 恒 "admin" 会让 EE 迁移来的团队文件对所有登录用户可读——必须是 "none"。
    return "none"


def has_permission(current: PermissionLevel, required: PermissionLevel) -> bool:
    return _RANK[current] >= _RANK[required]


def resolve_artifact_access(db: Session, user_id: str, owner_id, team_id) -> PermissionLevel:
    """owner ∪ team 合成的 artifact 访问级（与 EE 版同签名）。

    CE：owner 恒 admin；团队通道恒 none——自己的资源（含 EE 迁移来的
    team_id 标记文件）始终可访问，他人的团队资源不可见。
    """
    if owner_id and str(owner_id) == str(user_id):
        return "admin"
    if team_id:
        return resolve_team_file_permission(db, str(user_id), str(team_id))
    return "none"


def require_team_file_permission(
    db: Session,
    user_id: str,
    team_id: str,
    required: PermissionLevel,
    *,
    request: Any = None,
    action: str = "team_file.access",
) -> PermissionLevel:
    raise HTTPException(status_code=404, detail="团队功能在当前版本不可用")


# ── 项目权限（CE：个人项目 owner 判定保留） ───────────────────────────────────

ProjectPermissionLevel = Literal["none", "view", "edit", "admin"]


def resolve_project_permission(db: Session, user_id: str, project: Project) -> ProjectPermissionLevel:
    if project is None or project.deleted_at is not None:
        return "none"
    if project.kind == "personal":
        return "admin" if project.owner_user_id == user_id else "none"
    # CE 不存在团队项目；历史数据兜底为不可见
    return "none"


def can_create_team_project(db: Session, user_id: str, team_id: str) -> bool:
    return True


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


# ── 会话共享权限（CE：owner-only） ───────────────────────────────────────────

ChatAccessLevel = Literal["none", "read", "edit", "admin"]


def resolve_chat_access(db: Session, user_id: str, session: ChatSession) -> ChatAccessLevel:
    if session is None or session.deleted_at is not None:
        return "none"
    return "admin" if session.user_id == user_id else "none"


def can_modify_share_scope(db: Session, user_id: str, session: ChatSession) -> bool:
    if session is None or session.deleted_at is not None:
        return False
    return session.user_id == user_id


def can_delete_session(db: Session, user_id: str, session: ChatSession) -> bool:
    if session is None or session.deleted_at is not None:
        return False
    return session.user_id == user_id


__all__ = [
    "ChatAccessLevel",
    "can_delete_session",
    "can_modify_share_scope",
    "resolve_chat_access",
    "ProjectAccess",
    "ProjectPermissionLevel",
    "can_create_team_project",
    "require_project_access",
    "resolve_project_permission",
    "PermissionLevel",
    "has_permission",
    "require_team_file_permission",
    "resolve_artifact_access",
    "resolve_team_file_permission",
]
