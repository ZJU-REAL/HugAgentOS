"""Project (Claude-style workspace) business layer.

Handles project CRUD, listing (with mixed personal+team visibility), favorites,
and activity refresh. File-related operations live in
:mod:`core.services.project_file_service`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, desc, func, or_
from sqlalchemy.orm import Session

from core.auth.permissions_iface import (
    ProjectPermissionLevel,
    can_create_team_project,
    resolve_project_permission,
)
from core.db.models import (
    Artifact,
    ChatSession,
    ChatSessionUserState,
    Project,
    ProjectFavorite,
    Team,
    TeamFolder,
    TeamMember,
    UserFolder,
    UserShadow,
)
from core.db.repository import AuditLogRepository


@dataclass
class ProjectWithAccess:
    project: Project
    level: ProjectPermissionLevel
    favorite: bool


def _next_available_folder_name(base: str, existing: List) -> str:
    """Pick a non-conflicting folder name under the root (appending (2), (3) suffixes).

    ``existing`` is query rows of the shape ``[(name,), ...]``.
    """
    base = (base or "").strip() or "新项目"
    used = {row[0] for row in (existing or [])}
    if base not in used:
        return base
    n = 2
    while True:
        candidate = f"{base} ({n})"
        if candidate not in used:
            return candidate
        n += 1


def _project_to_summary(
    project: Project,
    *,
    level: ProjectPermissionLevel,
    favorite: bool,
    team_name: Optional[str] = None,
    folder_name: Optional[str] = None,
    file_count: int = 0,
    chat_count: int = 0,
) -> Dict[str, Any]:
    """Unified serialization (shared by list / get)."""
    extra = project.extra_data or {}
    return {
        "project_id": project.project_id,
        "name": project.name,
        "description": project.description or "",
        "kind": project.kind,
        "owner_user_id": project.owner_user_id,
        "team_id": project.team_id,
        "team_name": team_name,
        "linked_folder_id": project.linked_folder_id,
        "linked_team_folder_id": project.linked_team_folder_id,
        "folder_name": folder_name,
        "instructions": project.instructions or "",
        "icon_color": project.icon_color,
        "pinned": bool(project.pinned),
        "favorite": favorite,
        # Project-level memory read/write switches: default ON when absent (both new and legacy projects count as enabled).
        "memory_enabled": bool(extra.get("memory_enabled", True)),
        "memory_write_enabled": bool(extra.get("memory_write_enabled", True)),
        "permission": level,
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
        self.audit_repo = AuditLogRepository(db)

    # ── Visibility helpers ────────────────────────────────────────────
    def _visible_team_ids(self, user_id: str) -> List[str]:
        rows = (
            self.db.query(TeamMember.team_id)
            .filter(TeamMember.user_id == user_id)
            .all()
        )
        return [r[0] for r in rows]

    # ── List ──────────────────────────────────────────────────────────
    def list_visible(
        self,
        user_id: str,
        *,
        q: Optional[str] = None,
        sort: str = "-last_activity_at",
        page: int = 1,
        page_size: int = 30,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """List projects visible to the current user (personal owner=me ∪ team where me∈team_members)."""
        team_ids = self._visible_team_ids(user_id)
        visibility = or_(
            and_(Project.kind == "personal", Project.owner_user_id == user_id),
            and_(Project.kind == "team", Project.team_id.in_(team_ids)) if team_ids
            else and_(Project.kind == "team", Project.team_id == "__never__"),
        )

        base_q = self.db.query(Project).filter(
            Project.deleted_at.is_(None),
            visibility,
        )
        if q:
            pattern = f"%{q.strip()}%"
            base_q = base_q.filter(
                or_(Project.name.ilike(pattern), Project.description.ilike(pattern))
            )

        total = base_q.count()

        # sort
        if sort == "name":
            base_q = base_q.order_by(Project.name.asc())
        elif sort == "created":
            base_q = base_q.order_by(desc(Project.created_at))
        else:  # default '-last_activity_at'
            base_q = base_q.order_by(desc(Project.pinned), desc(Project.last_activity_at))

        projects = (
            base_q.offset((page - 1) * page_size).limit(page_size).all()
        )

        # batch fetch: favorites, team names, chat counts (file_count is now computed per
        # folder subtree — small scale, calculated separately in the loop)
        project_ids = [p.project_id for p in projects]
        fav_ids = set()
        team_name_by_id: Dict[str, str] = {}
        chat_count_by_id: Dict[str, int] = {}
        folder_names_user: Dict[str, str] = {}
        folder_names_team: Dict[str, str] = {}
        if project_ids:
            fav_rows = (
                self.db.query(ProjectFavorite.project_id)
                .filter(
                    ProjectFavorite.user_id == user_id,
                    ProjectFavorite.project_id.in_(project_ids),
                )
                .all()
            )
            fav_ids = {r[0] for r in fav_rows}

            distinct_team_ids = [p.team_id for p in projects if p.team_id]
            if distinct_team_ids:
                t_rows = (
                    self.db.query(Team.team_id, Team.name)
                    .filter(Team.team_id.in_(distinct_team_ids))
                    .all()
                )
                team_name_by_id = {tid: name for tid, name in t_rows}

            # chat_count: chats I own ∪ chats shared by others within team projects
            team_project_ids = [p.project_id for p in projects if p.kind == "team"]
            cc_or_terms = [ChatSession.user_id == user_id]
            if team_project_ids:
                cc_or_terms.append(
                    and_(
                        ChatSession.project_id.in_(team_project_ids),
                        ChatSession.share_scope.in_(("team_read", "team_edit")),
                    )
                )
            cc_filter = or_(*cc_or_terms) if len(cc_or_terms) > 1 else cc_or_terms[0]
            cc_rows = (
                self.db.query(ChatSession.project_id, func.count(ChatSession.chat_id))
                .filter(
                    ChatSession.project_id.in_(project_ids),
                    ChatSession.deleted_at.is_(None),
                    cc_filter,
                )
                .group_by(ChatSession.project_id)
                .all()
            )
            chat_count_by_id = {pid: int(n) for pid, n in cc_rows}

            uf_ids = [p.linked_folder_id for p in projects if p.linked_folder_id]
            if uf_ids:
                folder_names_user = {
                    fid: name
                    for fid, name in self.db.query(UserFolder.folder_id, UserFolder.name)
                    .filter(UserFolder.folder_id.in_(uf_ids))
                    .all()
                }
            tf_ids = [p.linked_team_folder_id for p in projects if p.linked_team_folder_id]
            if tf_ids:
                folder_names_team = {
                    fid: name
                    for fid, name in self.db.query(TeamFolder.folder_id, TeamFolder.name)
                    .filter(TeamFolder.folder_id.in_(tf_ids))
                    .all()
                }

        items = [
            _project_to_summary(
                p,
                level=resolve_project_permission(self.db, user_id, p),
                favorite=(p.project_id in fav_ids),
                team_name=team_name_by_id.get(p.team_id) if p.team_id else None,
                folder_name=(
                    folder_names_user.get(p.linked_folder_id)
                    if p.kind == "personal"
                    else folder_names_team.get(p.linked_team_folder_id)
                ),
                file_count=self._count_files_in_subtree(p),
                chat_count=chat_count_by_id.get(p.project_id, 0),
            )
            for p in projects
        ]
        return items, total

    def _count_files_in_subtree(self, project: Project) -> int:
        """Count live artifacts under the subtree of the folder linked to this project."""
        if project.kind == "personal":
            if not project.linked_folder_id:
                return 0
            ids = self._user_folder_subtree_ids(project.linked_folder_id, project.owner_user_id)
            if not ids:
                return 0
            return int(
                self.db.query(func.count(Artifact.artifact_id))
                .filter(
                    Artifact.user_id == project.owner_user_id,
                    Artifact.team_id.is_(None),
                    Artifact.user_folder_id.in_(ids),
                    Artifact.deleted_at.is_(None),
                )
                .scalar() or 0
            )
        # team
        if not project.linked_team_folder_id or not project.team_id:
            return 0
        ids = self._team_folder_subtree_ids(project.linked_team_folder_id, project.team_id)
        if not ids:
            return 0
        return int(
            self.db.query(func.count(Artifact.artifact_id))
            .filter(
                Artifact.team_id == project.team_id,
                Artifact.team_folder_id.in_(ids),
                Artifact.deleted_at.is_(None),
            )
            .scalar() or 0
        )

    def _user_folder_subtree_ids(self, root_id: str, user_id: str) -> List[str]:
        out: List[str] = []
        stack = [root_id]
        seen: set[str] = set()
        guard = 0
        while stack and guard < 5000:
            guard += 1
            fid = stack.pop()
            if fid in seen:
                continue
            seen.add(fid)
            out.append(fid)
            children = (
                self.db.query(UserFolder.folder_id)
                .filter(
                    UserFolder.user_id == user_id,
                    UserFolder.parent_folder_id == fid,
                    UserFolder.deleted_at.is_(None),
                )
                .all()
            )
            stack.extend(r[0] for r in children)
        return out

    def _team_folder_subtree_ids(self, root_id: str, team_id: str) -> List[str]:
        out: List[str] = []
        stack = [root_id]
        seen: set[str] = set()
        guard = 0
        while stack and guard < 5000:
            guard += 1
            fid = stack.pop()
            if fid in seen:
                continue
            seen.add(fid)
            out.append(fid)
            children = (
                self.db.query(TeamFolder.folder_id)
                .filter(
                    TeamFolder.team_id == team_id,
                    TeamFolder.parent_folder_id == fid,
                    TeamFolder.deleted_at.is_(None),
                )
                .all()
            )
            stack.extend(r[0] for r in children)
        return out

    # ── Create ────────────────────────────────────────────────────────
    def create_personal(
        self,
        user_id: str,
        name: str,
        description: Optional[str] = None,
        *,
        linked_folder_id: Optional[str] = None,
    ) -> Project:
        """Create a personal project.

        If ``linked_folder_id`` is given, link to that existing personal folder;
        otherwise create a new user_folder named after the project at the personal
        folder root as the link target. One folder may be linked to only one live
        project (checked manually in the service layer to avoid relying on a
        PG-only partial unique index).
        """
        return self._create(
            user_id=user_id,
            kind="personal",
            name=name,
            description=description,
            team_id=None,
            linked_folder_id=linked_folder_id,
            linked_team_folder_id=None,
        )

    def create_team(
        self,
        user_id: str,
        team_id: str,
        name: str,
        description: Optional[str] = None,
        *,
        linked_team_folder_id: Optional[str] = None,
    ) -> Project:
        if not can_create_team_project(self.db, user_id, team_id):
            from fastapi import HTTPException
            self.audit_repo.log_denial(
                user_id=user_id,
                action="project.create_team",
                reason="not_team_admin",
                required="admin",
                actual="member_or_none",
                resource_type="team",
                resource_id=team_id,
            )
            raise HTTPException(status_code=403, detail="仅团队 owner / admin 可创建团队项目")
        return self._create(
            user_id=user_id,
            kind="team",
            name=name,
            description=description,
            team_id=team_id,
            linked_folder_id=None,
            linked_team_folder_id=linked_team_folder_id,
        )

    def _create(
        self,
        *,
        user_id: str,
        kind: str,
        name: str,
        description: Optional[str],
        team_id: Optional[str],
        linked_folder_id: Optional[str],
        linked_team_folder_id: Optional[str],
    ) -> Project:
        from fastapi import HTTPException

        clean = (name or "").strip()
        if not clean:
            raise HTTPException(status_code=400, detail="项目名不能为空")
        if len(clean) > 120:
            raise HTTPException(status_code=400, detail="项目名过长（≤120 字）")

        # Duplicate-name check: personal projects with the same owner / team projects within the same team may not share a name (live records)
        dup_q = self.db.query(Project.project_id).filter(
            Project.kind == kind,
            Project.name == clean,
            Project.deleted_at.is_(None),
        )
        if kind == "personal":
            dup_q = dup_q.filter(Project.owner_user_id == user_id)
        else:
            dup_q = dup_q.filter(Project.team_id == team_id)
        if dup_q.first() is not None:
            raise HTTPException(status_code=400, detail="同名项目已存在")

        # ── Resolve or create the linked folder ────────────────
        if kind == "personal":
            if linked_folder_id:
                fld = (
                    self.db.query(UserFolder)
                    .filter(
                        UserFolder.folder_id == linked_folder_id,
                        UserFolder.user_id == user_id,
                        UserFolder.deleted_at.is_(None),
                    )
                    .first()
                )
                if fld is None:
                    raise HTTPException(status_code=400, detail="目标文件夹不存在")
                if self._user_folder_already_linked(linked_folder_id):
                    raise HTTPException(status_code=400, detail="该文件夹已被其它项目挂钩")
            else:
                # Auto-create a personal folder with the same name (appending a -N suffix on collision)
                folder_name = self._unique_personal_folder_name(user_id, clean)
                res = self._get_folder_service("personal").create_folder(
                    user_id=user_id,
                    parent_folder_id=None,
                    name=folder_name,
                    actor=user_id,
                )
                if not res.ok or not res.folder_id:
                    raise HTTPException(status_code=400, detail=res.message or "新建项目文件夹失败")
                linked_folder_id = res.folder_id
        else:  # team
            if linked_team_folder_id:
                tfld = (
                    self.db.query(TeamFolder)
                    .filter(
                        TeamFolder.folder_id == linked_team_folder_id,
                        TeamFolder.team_id == team_id,
                        TeamFolder.deleted_at.is_(None),
                    )
                    .first()
                )
                if tfld is None:
                    raise HTTPException(status_code=400, detail="目标团队文件夹不存在")
                if self._team_folder_already_linked(linked_team_folder_id):
                    raise HTTPException(status_code=400, detail="该团队文件夹已被其它项目挂钩")
            else:
                folder_name = self._unique_team_folder_name(team_id, clean)
                res = self._get_folder_service("team").create_folder(
                    team_id=team_id,
                    parent_folder_id=None,
                    name=folder_name,
                    actor=user_id,
                )
                if not res.ok or not res.folder_id:
                    raise HTTPException(status_code=400, detail=res.message or "新建团队项目文件夹失败")
                linked_team_folder_id = res.folder_id

        project = Project(
            project_id=f"prj_{uuid.uuid4().hex[:16]}",
            name=clean,
            description=(description or "").strip() or None,
            kind=kind,
            owner_user_id=user_id,
            team_id=team_id,
            linked_folder_id=linked_folder_id,
            linked_team_folder_id=linked_team_folder_id,
            instructions=None,
            icon_color=None,
            pinned=False,
            extra_data={},
        )
        now = datetime.utcnow()
        project.created_at = now
        project.updated_at = now
        project.last_activity_at = now
        self.db.add(project)
        self.db.commit()
        self.db.refresh(project)

        self.audit_repo.create({
            "user_id": user_id,
            "action": "project.created",
            "resource_type": "project",
            "resource_id": project.project_id,
            "details": {
                "kind": kind,
                "team_id": team_id,
                "name": clean,
                "linked_folder_id": linked_folder_id,
                "linked_team_folder_id": linked_team_folder_id,
            },
            "status": "success",
        })
        return project

    # ── Folder helpers ────────────────────────────────────────────────
    def _get_folder_service(self, kind: str):
        """Folder service factory (seam C6).

        Imports are kept inside the branches: the CE derived tree does not contain
        team_folder_service; in single-tenant mode kind is always personal, so the
        team branch is never reached.
        """
        if kind == "personal":
            from core.services.user_folder_service import UserFolderService
            return UserFolderService(self.db)
        from core.services.team_folder_service import TeamFolderService
        return TeamFolderService(self.db)

    def _user_folder_already_linked(self, folder_id: str) -> bool:
        return (
            self.db.query(Project.project_id)
            .filter(
                Project.linked_folder_id == folder_id,
                Project.deleted_at.is_(None),
            )
            .first()
            is not None
        )

    def _team_folder_already_linked(self, folder_id: str) -> bool:
        return (
            self.db.query(Project.project_id)
            .filter(
                Project.linked_team_folder_id == folder_id,
                Project.deleted_at.is_(None),
            )
            .first()
            is not None
        )

    def _unique_personal_folder_name(self, user_id: str, base: str) -> str:
        return _next_available_folder_name(
            base,
            existing=self.db.query(UserFolder.name)
            .filter(
                UserFolder.user_id == user_id,
                UserFolder.parent_folder_id.is_(None),
                UserFolder.deleted_at.is_(None),
            )
            .all(),
        )

    def _unique_team_folder_name(self, team_id: str, base: str) -> str:
        return _next_available_folder_name(
            base,
            existing=self.db.query(TeamFolder.name)
            .filter(
                TeamFolder.team_id == team_id,
                TeamFolder.parent_folder_id.is_(None),
                TeamFolder.deleted_at.is_(None),
            )
            .all(),
        )

    # ── Read ──────────────────────────────────────────────────────────
    def get(
        self, project_id: str, user_id: str
    ) -> Optional[Dict[str, Any]]:
        project = (
            self.db.query(Project)
            .filter(Project.project_id == project_id, Project.deleted_at.is_(None))
            .first()
        )
        if project is None:
            return None
        level = resolve_project_permission(self.db, user_id, project)
        if level == "none":
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
        team_name = None
        if project.team_id:
            t = self.db.query(Team.name).filter(Team.team_id == project.team_id).first()
            team_name = t[0] if t else None
        folder_name: Optional[str] = None
        if project.kind == "personal" and project.linked_folder_id:
            row = (
                self.db.query(UserFolder.name)
                .filter(UserFolder.folder_id == project.linked_folder_id)
                .first()
            )
            folder_name = row[0] if row else None
        elif project.kind == "team" and project.linked_team_folder_id:
            row = (
                self.db.query(TeamFolder.name)
                .filter(TeamFolder.folder_id == project.linked_team_folder_id)
                .first()
            )
            folder_name = row[0] if row else None
        file_count = self._count_files_in_subtree(project)
        # Team project: count includes my own ∪ chats in the project with share_scope ∈ (team_read, team_edit);
        # personal project / non-project: only my own
        chat_count_q = self.db.query(func.count(ChatSession.chat_id)).filter(
            ChatSession.project_id == project_id,
            ChatSession.deleted_at.is_(None),
        )
        if project.kind == "team":
            chat_count_q = chat_count_q.filter(
                or_(
                    ChatSession.user_id == user_id,
                    ChatSession.share_scope.in_(("team_read", "team_edit")),
                )
            )
        else:
            chat_count_q = chat_count_q.filter(ChatSession.user_id == user_id)
        chat_count = chat_count_q.scalar() or 0
        return _project_to_summary(
            project,
            level=level,
            favorite=favorite,
            team_name=team_name,
            folder_name=folder_name,
            file_count=int(file_count),
            chat_count=int(chat_count),
        )

    def get_raw(self, project_id: str) -> Optional[Project]:
        """Fetch the raw ORM row without authorization (for the permission layer / internal use only)."""
        return (
            self.db.query(Project)
            .filter(Project.project_id == project_id, Project.deleted_at.is_(None))
            .first()
        )

    # ── Update ────────────────────────────────────────────────────────
    def update(
        self,
        project_id: str,
        user_id: str,
        patch: Dict[str, Any],
        *,
        level: ProjectPermissionLevel,
    ) -> Optional[Dict[str, Any]]:
        from fastapi import HTTPException

        project = self.get_raw(project_id)
        if project is None:
            return None

        # admin-only fields
        ADMIN_FIELDS = {"name", "pinned", "icon_color"}
        EDIT_FIELDS = {"description", "instructions", "memory_enabled", "memory_write_enabled"}
        for key in patch.keys():
            if key in ADMIN_FIELDS and level != "admin":
                raise HTTPException(status_code=403, detail=f"仅项目管理员可修改 {key}")
            if key not in ADMIN_FIELDS and key not in EDIT_FIELDS:
                raise HTTPException(status_code=400, detail=f"不支持修改字段: {key}")

        if "name" in patch:
            clean = (patch["name"] or "").strip()
            if not clean:
                raise HTTPException(status_code=400, detail="项目名不能为空")
            if len(clean) > 120:
                raise HTTPException(status_code=400, detail="项目名过长（≤120 字）")
            # Duplicate-name check (excluding itself)
            dup_q = self.db.query(Project.project_id).filter(
                Project.kind == project.kind,
                Project.name == clean,
                Project.deleted_at.is_(None),
                Project.project_id != project_id,
            )
            if project.kind == "personal":
                dup_q = dup_q.filter(Project.owner_user_id == project.owner_user_id)
            else:
                dup_q = dup_q.filter(Project.team_id == project.team_id)
            if dup_q.first() is not None:
                raise HTTPException(status_code=400, detail="同名项目已存在")
            project.name = clean

        if "description" in patch:
            project.description = (patch["description"] or "").strip() or None
        if "instructions" in patch:
            instructions = patch["instructions"] or ""
            # Simple cap to keep the system prompt from getting too long
            if len(instructions) > 8000:
                raise HTTPException(status_code=400, detail="项目指令过长（≤8000 字符）")
            project.instructions = instructions.strip() or None
        if "pinned" in patch:
            project.pinned = bool(patch["pinned"])
        if "icon_color" in patch:
            color = (patch["icon_color"] or "").strip() or None
            if color and len(color) > 20:
                raise HTTPException(status_code=400, detail="颜色字符串过长（≤20 字符）")
            project.icon_color = color
        # Memory switches are stored in extra_data (JSONB), updated only when explicitly
        # passed; reads default to True when absent. Reassign the whole dict to trigger
        # SQLAlchemy change detection (JSONType is not MutableDict).
        if "memory_enabled" in patch or "memory_write_enabled" in patch:
            new_extra = dict(project.extra_data or {})
            if "memory_enabled" in patch:
                new_extra["memory_enabled"] = bool(patch["memory_enabled"])
            if "memory_write_enabled" in patch:
                new_extra["memory_write_enabled"] = bool(patch["memory_write_enabled"])
            project.extra_data = new_extra

        project.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(project)

        self.audit_repo.create({
            "user_id": user_id,
            "action": "project.updated",
            "resource_type": "project",
            "resource_id": project_id,
            "details": {k: v for k, v in patch.items() if k != "instructions"},  # content excluded from audit
            "status": "success",
        })
        return self.get(project_id, user_id)

    def soft_delete(self, project_id: str, user_id: str) -> bool:
        project = self.get_raw(project_id)
        if project is None:
            return False
        project.deleted_at = datetime.utcnow()
        self.db.commit()
        self.audit_repo.create({
            "user_id": user_id,
            "action": "project.deleted",
            "resource_type": "project",
            "resource_id": project_id,
            "status": "success",
        })
        return True

    # ── Favorite ──────────────────────────────────────────────────────
    def toggle_favorite(self, project_id: str, user_id: str, on: bool) -> bool:
        existing = (
            self.db.query(ProjectFavorite)
            .filter(
                ProjectFavorite.project_id == project_id,
                ProjectFavorite.user_id == user_id,
            )
            .first()
        )
        if on and existing is None:
            self.db.add(ProjectFavorite(project_id=project_id, user_id=user_id))
            self.db.commit()
        elif (not on) and existing is not None:
            self.db.delete(existing)
            self.db.commit()
        return on

    # ── Activity ──────────────────────────────────────────────────────
    def touch_activity(self, project_id: str) -> None:
        project = self.get_raw(project_id)
        if project is None:
            return
        project.last_activity_at = datetime.utcnow()
        self.db.commit()

    # ── Chats listing within a project ────────────────────────────────
    def list_chats(
        self,
        project_id: str,
        user_id: str,
        page: int = 1,
        page_size: int = 30,
        scope: str = "all",
    ) -> Tuple[List[Dict[str, Any]], int]:
        """List chats within a project.

        - Personal project: only chats owned by the current user.
        - Team project: my own chats, unioned with chats other members have shared by
          setting ``share_scope`` to ``team_read`` / ``team_edit``; pin/favorite go
          through the ``chat_session_user_states`` table (independent per user).
        - ``scope`` filter: ``all`` / ``mine`` / ``shared`` (only meaningful for team projects).
        """
        project = self.get_raw(project_id)
        if project is None:
            return [], 0

        # Team projects always render with shared-list semantics
        team_share = project.kind == "team"

        base = self.db.query(ChatSession).filter(
            ChatSession.project_id == project_id,
            ChatSession.deleted_at.is_(None),
        )
        if team_share:
            if scope == "mine":
                base = base.filter(ChatSession.user_id == user_id)
            elif scope == "shared":
                base = base.filter(
                    ChatSession.user_id != user_id,
                    ChatSession.share_scope.in_(("team_read", "team_edit")),
                )
            else:  # 'all'
                base = base.filter(
                    or_(
                        ChatSession.user_id == user_id,
                        ChatSession.share_scope.in_(("team_read", "team_edit")),
                    )
                )
        else:
            base = base.filter(ChatSession.user_id == user_id)

        total = base.count()
        rows = (
            base.order_by(desc(ChatSession.updated_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )

        # owner_id → username in one query
        owner_ids = {s.user_id for s in rows}
        owner_name_map: Dict[str, str] = {}
        if owner_ids:
            for uid, uname in (
                self.db.query(UserShadow.user_id, UserShadow.username)
                .filter(UserShadow.user_id.in_(owner_ids))
                .all()
            ):
                owner_name_map[uid] = uname

        # The current user's per-user state for this batch of chat_ids (fetched only when team_share)
        user_state_map: Dict[str, ChatSessionUserState] = {}
        if team_share and rows:
            chat_ids = [s.chat_id for s in rows]
            for state in (
                self.db.query(ChatSessionUserState)
                .filter(
                    ChatSessionUserState.user_id == user_id,
                    ChatSessionUserState.chat_id.in_(chat_ids),
                )
                .all()
            ):
                user_state_map[state.chat_id] = state

        items: List[Dict[str, Any]] = []
        for s in rows:
            is_owner = s.user_id == user_id
            if team_share:
                state = user_state_map.get(s.chat_id)
                pinned = bool(state.pinned) if state is not None else False
                favorite = bool(state.favorite) if state is not None else False
            else:
                pinned = bool(s.pinned)
                favorite = bool(s.favorite)
            items.append({
                "chat_id": s.chat_id,
                "title": s.title,
                "pinned": pinned,
                "favorite": favorite,
                "message_count": int(s.message_count or 0),
                "last_message_at": s.last_message_at.isoformat() if s.last_message_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "project_id": s.project_id,
                "owner_user_id": s.user_id,
                "owner_name": owner_name_map.get(s.user_id),
                "share_scope": s.share_scope or "private",
                "is_owner": is_owner,
            })
        return items, total

    # ── Per-user chat state (team-share scenario) ─────────────────────
    def upsert_chat_user_state(
        self,
        chat_id: str,
        user_id: str,
        *,
        pinned: Optional[bool] = None,
        favorite: Optional[bool] = None,
    ) -> ChatSessionUserState:
        """Upsert the current user's pin/favorite for a chat on ``chat_session_user_states``.

        - Only patch the fields explicitly passed in.
        - If no row exists, insert a new one (defaults false) with the passed fields applied.
        """
        state = (
            self.db.query(ChatSessionUserState)
            .filter(
                ChatSessionUserState.chat_id == chat_id,
                ChatSessionUserState.user_id == user_id,
            )
            .first()
        )
        if state is None:
            state = ChatSessionUserState(
                chat_id=chat_id,
                user_id=user_id,
                pinned=bool(pinned) if pinned is not None else False,
                favorite=bool(favorite) if favorite is not None else False,
            )
            self.db.add(state)
        else:
            if pinned is not None:
                state.pinned = bool(pinned)
            if favorite is not None:
                state.favorite = bool(favorite)
            state.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(state)
        return state

    def get_chat_user_state(
        self, chat_id: str, user_id: str
    ) -> Optional[ChatSessionUserState]:
        return (
            self.db.query(ChatSessionUserState)
            .filter(
                ChatSessionUserState.chat_id == chat_id,
                ChatSessionUserState.user_id == user_id,
            )
            .first()
        )

    # ── Helpers for cross-module use ──────────────────────────────────
    def list_teams_user_can_create_in(self, user_id: str) -> List[Dict[str, Any]]:
        """Frontend create-project modal: only show teams where the user is owner / admin."""
        rows = (
            self.db.query(Team, TeamMember.role)
            .join(TeamMember, Team.team_id == TeamMember.team_id)
            .filter(
                TeamMember.user_id == user_id,
                TeamMember.role.in_(("owner", "admin")),
            )
            .order_by(Team.name)
            .all()
        )
        return [{"team_id": t.team_id, "name": t.name, "role": role} for t, role in rows]
