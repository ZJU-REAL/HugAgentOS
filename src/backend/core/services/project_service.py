"""Personal-project service for Community Edition."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.auth.permissions_iface import ProjectPermissionLevel, resolve_project_permission
from core.db.models import Artifact, ChatSession, Project, ProjectFavorite, UserFolder, UserShadow
from fastapi import HTTPException
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session


def _next_available_folder_name(base: str, existing: List) -> str:
    base = (base or "").strip() or "新项目"
    used = {row[0] for row in existing or []}
    if base not in used:
        return base
    index = 2
    while f"{base} ({index})" in used:
        index += 1
    return f"{base} ({index})"


def _summary(
    project: Project,
    *,
    favorite: bool,
    folder_name: Optional[str],
    file_count: int,
    chat_count: int,
) -> Dict[str, Any]:
    extra = project.extra_data or {}
    return {
        "project_id": project.project_id,
        "name": project.name,
        "description": project.description or "",
        "kind": "personal",
        "owner_user_id": project.owner_user_id,
        "linked_folder_id": project.linked_folder_id,
        "folder_name": folder_name,
        "instructions": project.instructions or "",
        "icon_color": project.icon_color,
        "pinned": bool(project.pinned),
        "favorite": favorite,
        "memory_enabled": bool(extra.get("memory_enabled", True)),
        "memory_write_enabled": bool(extra.get("memory_write_enabled", True)),
        "permission": "admin",
        "file_count": file_count,
        "chat_count": chat_count,
        "metadata": extra,
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "updated_at": project.updated_at.isoformat() if project.updated_at else None,
        "last_activity_at": (
            project.last_activity_at.isoformat() if project.last_activity_at else None
        ),
    }


class ProjectService:
    def __init__(self, db: Session):
        self.db = db

    def _subtree_ids(self, root_id: str, user_id: str) -> List[str]:
        out: List[str] = []
        stack = [root_id]
        seen: set[str] = set()
        while stack and len(seen) < 5000:
            folder_id = stack.pop()
            if folder_id in seen:
                continue
            seen.add(folder_id)
            out.append(folder_id)
            children = (
                self.db.query(UserFolder.folder_id)
                .filter(
                    UserFolder.user_id == user_id,
                    UserFolder.parent_folder_id == folder_id,
                    UserFolder.deleted_at.is_(None),
                )
                .all()
            )
            stack.extend(row[0] for row in children)
        return out

    def _file_count(self, project: Project) -> int:
        if not project.linked_folder_id:
            return 0
        folder_ids = self._subtree_ids(project.linked_folder_id, project.owner_user_id)
        return int(
            self.db.query(func.count(Artifact.artifact_id))
            .filter(
                Artifact.user_id == project.owner_user_id,
                Artifact.user_folder_id.in_(folder_ids),
                Artifact.deleted_at.is_(None),
            )
            .scalar()
            or 0
        )

    def list_visible(
        self,
        user_id: str,
        *,
        q: Optional[str] = None,
        sort: str = "-last_activity_at",
        page: int = 1,
        page_size: int = 30,
    ) -> Tuple[List[Dict[str, Any]], int]:
        query = self.db.query(Project).filter(
            Project.kind == "personal",
            Project.owner_user_id == user_id,
            Project.deleted_at.is_(None),
        )
        if q:
            pattern = f"%{q.strip()}%"
            query = query.filter(
                or_(Project.name.ilike(pattern), Project.description.ilike(pattern))
            )
        total = query.count()
        if sort == "name":
            query = query.order_by(Project.name.asc())
        elif sort == "created":
            query = query.order_by(desc(Project.created_at))
        else:
            query = query.order_by(desc(Project.pinned), desc(Project.last_activity_at))
        projects = query.offset((page - 1) * page_size).limit(page_size).all()
        ids = [project.project_id for project in projects]
        favorite_ids = (
            {
                row[0]
                for row in self.db.query(ProjectFavorite.project_id)
                .filter(
                    ProjectFavorite.user_id == user_id,
                    ProjectFavorite.project_id.in_(ids),
                )
                .all()
            }
            if ids
            else set()
        )
        chat_counts = (
            {
                project_id: int(count)
                for project_id, count in self.db.query(
                    ChatSession.project_id, func.count(ChatSession.chat_id)
                )
                .filter(
                    ChatSession.project_id.in_(ids),
                    ChatSession.user_id == user_id,
                    ChatSession.deleted_at.is_(None),
                )
                .group_by(ChatSession.project_id)
                .all()
            }
            if ids
            else {}
        )
        folder_ids = [project.linked_folder_id for project in projects if project.linked_folder_id]
        folder_names = (
            {
                folder_id: name
                for folder_id, name in self.db.query(UserFolder.folder_id, UserFolder.name)
                .filter(UserFolder.folder_id.in_(folder_ids))
                .all()
            }
            if folder_ids
            else {}
        )
        return [
            _summary(
                project,
                favorite=project.project_id in favorite_ids,
                folder_name=folder_names.get(project.linked_folder_id),
                file_count=self._file_count(project),
                chat_count=chat_counts.get(project.project_id, 0),
            )
            for project in projects
        ], total

    def create_personal(
        self,
        user_id: str,
        name: str,
        description: Optional[str] = None,
        *,
        linked_folder_id: Optional[str] = None,
    ) -> Project:
        clean = (name or "").strip()
        if not clean:
            raise HTTPException(status_code=400, detail="项目名不能为空")
        if len(clean) > 120:
            raise HTTPException(status_code=400, detail="项目名过长（≤120 字）")
        duplicate = (
            self.db.query(Project.project_id)
            .filter(
                Project.kind == "personal",
                Project.owner_user_id == user_id,
                Project.name == clean,
                Project.deleted_at.is_(None),
            )
            .first()
        )
        if duplicate:
            raise HTTPException(status_code=400, detail="同名项目已存在")
        if linked_folder_id:
            folder = (
                self.db.query(UserFolder)
                .filter(
                    UserFolder.folder_id == linked_folder_id,
                    UserFolder.user_id == user_id,
                    UserFolder.deleted_at.is_(None),
                )
                .first()
            )
            if folder is None:
                raise HTTPException(status_code=400, detail="目标文件夹不存在")
            if (
                self.db.query(Project.project_id)
                .filter(
                    Project.linked_folder_id == linked_folder_id,
                    Project.deleted_at.is_(None),
                )
                .first()
            ):
                raise HTTPException(status_code=400, detail="该文件夹已被其它项目挂钩")
        else:
            from core.services.user_folder_service import UserFolderService

            folder_name = _next_available_folder_name(
                clean,
                self.db.query(UserFolder.name)
                .filter(
                    UserFolder.user_id == user_id,
                    UserFolder.parent_folder_id.is_(None),
                    UserFolder.deleted_at.is_(None),
                )
                .all(),
            )
            result = UserFolderService(self.db).create_folder(
                user_id=user_id, parent_folder_id=None, name=folder_name, actor=user_id
            )
            if not result.ok or not result.folder_id:
                raise HTTPException(status_code=400, detail=result.message or "新建项目文件夹失败")
            linked_folder_id = result.folder_id
        now = datetime.utcnow()
        project = Project(
            project_id=f"prj_{uuid.uuid4().hex[:16]}",
            name=clean,
            description=(description or "").strip() or None,
            kind="personal",
            owner_user_id=user_id,
            linked_folder_id=linked_folder_id,
            extra_data={},
            pinned=False,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )
        self.db.add(project)
        self.db.commit()
        self.db.refresh(project)
        return project

    def get_raw(self, project_id: str) -> Optional[Project]:
        return (
            self.db.query(Project)
            .filter(
                Project.project_id == project_id,
                Project.kind == "personal",
                Project.deleted_at.is_(None),
            )
            .first()
        )

    def get(self, project_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        project = self.get_raw(project_id)
        if project is None or resolve_project_permission(self.db, user_id, project) == "none":
            return None
        favorite = (
            self.db.query(ProjectFavorite)
            .filter(
                ProjectFavorite.project_id == project_id,
                ProjectFavorite.user_id == user_id,
            )
            .first()
            is not None
        )
        folder = (
            self.db.query(UserFolder.name)
            .filter(UserFolder.folder_id == project.linked_folder_id)
            .first()
            if project.linked_folder_id
            else None
        )
        chat_count = int(
            self.db.query(func.count(ChatSession.chat_id))
            .filter(
                ChatSession.project_id == project_id,
                ChatSession.user_id == user_id,
                ChatSession.deleted_at.is_(None),
            )
            .scalar()
            or 0
        )
        return _summary(
            project,
            favorite=favorite,
            folder_name=folder[0] if folder else None,
            file_count=self._file_count(project),
            chat_count=chat_count,
        )

    def update(
        self,
        project_id: str,
        user_id: str,
        patch: Dict[str, Any],
        *,
        level: ProjectPermissionLevel,
    ) -> Optional[Dict[str, Any]]:
        project = self.get_raw(project_id)
        if project is None:
            return None
        admin_fields = {"name", "pinned", "icon_color"}
        edit_fields = {"description", "instructions", "memory_enabled", "memory_write_enabled"}
        for key in patch:
            if key in admin_fields and level != "admin":
                raise HTTPException(status_code=403, detail=f"仅项目管理员可修改 {key}")
            if key not in admin_fields | edit_fields:
                raise HTTPException(status_code=400, detail=f"不支持修改字段: {key}")
        if "name" in patch:
            clean = (patch["name"] or "").strip()
            if not clean or len(clean) > 120:
                raise HTTPException(status_code=400, detail="项目名不合法")
            duplicate = (
                self.db.query(Project.project_id)
                .filter(
                    Project.kind == "personal",
                    Project.owner_user_id == project.owner_user_id,
                    Project.name == clean,
                    Project.project_id != project_id,
                    Project.deleted_at.is_(None),
                )
                .first()
            )
            if duplicate:
                raise HTTPException(status_code=400, detail="同名项目已存在")
            project.name = clean
        if "description" in patch:
            project.description = (patch["description"] or "").strip() or None
        if "instructions" in patch:
            value = patch["instructions"] or ""
            if len(value) > 8000:
                raise HTTPException(status_code=400, detail="项目指令过长（≤8000 字符）")
            project.instructions = value.strip() or None
        if "pinned" in patch:
            project.pinned = bool(patch["pinned"])
        if "icon_color" in patch:
            project.icon_color = (patch["icon_color"] or "").strip() or None
        if "memory_enabled" in patch or "memory_write_enabled" in patch:
            extra = dict(project.extra_data or {})
            for key in ("memory_enabled", "memory_write_enabled"):
                if key in patch:
                    extra[key] = bool(patch[key])
            project.extra_data = extra
        project.updated_at = datetime.utcnow()
        self.db.commit()
        return self.get(project_id, user_id)

    def soft_delete(self, project_id: str, user_id: str) -> bool:
        project = self.get_raw(project_id)
        if project is None:
            return False
        project.deleted_at = datetime.utcnow()
        self.db.commit()
        return True

    def toggle_favorite(self, project_id: str, user_id: str, on: bool) -> bool:
        row = (
            self.db.query(ProjectFavorite)
            .filter(
                ProjectFavorite.project_id == project_id,
                ProjectFavorite.user_id == user_id,
            )
            .first()
        )
        if on and row is None:
            self.db.add(ProjectFavorite(project_id=project_id, user_id=user_id))
        elif not on and row is not None:
            self.db.delete(row)
        self.db.commit()
        return on

    def touch_activity(self, project_id: str) -> None:
        project = self.get_raw(project_id)
        if project is not None:
            project.last_activity_at = datetime.utcnow()
            self.db.commit()

    def list_chats(
        self,
        project_id: str,
        user_id: str,
        page: int = 1,
        page_size: int = 30,
        scope: str = "all",
    ) -> Tuple[List[Dict[str, Any]], int]:
        query = self.db.query(ChatSession).filter(
            ChatSession.project_id == project_id,
            ChatSession.user_id == user_id,
            ChatSession.deleted_at.is_(None),
        )
        total = query.count()
        rows = (
            query.order_by(desc(ChatSession.updated_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        owner = self.db.query(UserShadow.username).filter(UserShadow.user_id == user_id).first()
        return [
            {
                "chat_id": row.chat_id,
                "title": row.title,
                "pinned": bool(row.pinned),
                "favorite": bool(row.favorite),
                "message_count": int(row.message_count or 0),
                "last_message_at": row.last_message_at.isoformat() if row.last_message_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "project_id": row.project_id,
                "owner_user_id": row.user_id,
                "owner_name": owner[0] if owner else None,
                "is_owner": True,
            }
            for row in rows
        ], total


__all__ = ["ProjectService"]
