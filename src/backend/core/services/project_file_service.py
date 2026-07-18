"""Project files = artifacts under the subtree of a linked folder (personal / team).

Design principles:
- A project itself does not "own" files; it is merely a view of some MySpace folder.
- Upload / delete / create-subfolder all go entirely through MySpace's existing
  services (``UserFolderService`` / ``TeamFolderService`` + the file_upload route).
- This service is only responsible for browsing (listing the subtree), a convenience
  wrapper for creating subfolders, and capacity statistics.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.db.models import (
    Artifact,
    Project,
    TeamFolder,
    UserFolder,
)
from core.db.repository import AuditLogRepository
from core.storage import get_storage

logger = logging.getLogger(__name__)

_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_DEFAULT_PROJECT_CAPACITY_BYTES = 200 * 1024 * 1024  # 200 MB


def _capacity_limit() -> int:
    raw = os.getenv("PROJECT_FILE_CAPACITY_BYTES")
    if not raw:
        return _DEFAULT_PROJECT_CAPACITY_BYTES
    try:
        n = int(raw)
        return max(n, 1)
    except (TypeError, ValueError):
        return _DEFAULT_PROJECT_CAPACITY_BYTES


def _artifact_to_dict(art: Artifact, folder_path: str) -> Dict[str, Any]:
    return {
        "id": art.artifact_id,
        "artifact_id": art.artifact_id,
        # Relative path within the project: subfolder/file.ext or file.ext
        "name": (folder_path + "/" + art.filename) if folder_path else art.filename,
        "title": art.title,
        "mime_type": art.mime_type,
        "size_bytes": int(art.size_bytes or 0),
        "download_url": f"/files/{art.artifact_id}",
        "type": art.type,
        "folder_path": folder_path,
        "created_at": (art.updated_at or art.created_at).isoformat() if (art.updated_at or art.created_at) else None,
    }


class ProjectFileService:
    """Project file browsing / creating subfolders / capacity statistics."""

    def __init__(self, db: Session):
        self.db = db
        self.audit_repo = AuditLogRepository(db)

    # ── Read ──────────────────────────────────────────────────────────
    def list_files(self, project: Project) -> List[Dict[str, Any]]:
        """Recursively traverse all live artifacts under the linked folder's subtree,
        returned flattened by ``folder_path``.

        ``folder_path`` looks like ``"q1"`` or ``"q1/sub"``, relative to the linked
        folder root. The frontend can aggregate by the first path segment to form
        folder cards.
        """
        if project.kind == "personal":
            if not project.linked_folder_id:
                return []
            return self._list_user_subtree(project.owner_user_id, project.linked_folder_id)
        if project.kind == "team":
            if not project.linked_team_folder_id or not project.team_id:
                return []
            return self._list_team_subtree(project.team_id, project.linked_team_folder_id)
        return []

    def _list_user_subtree(self, user_id: str, root_id: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        stack: List[Tuple[str, str]] = [(root_id, "")]  # (folder_id, path_rel_to_root)
        guard = 0
        while stack and guard < 5000:
            guard += 1
            fid, path = stack.pop()
            arts = (
                self.db.query(Artifact)
                .filter(
                    Artifact.user_id == user_id,
                    Artifact.team_id.is_(None),
                    Artifact.user_folder_id == fid,
                    Artifact.deleted_at.is_(None),
                )
                .order_by(Artifact.created_at.desc())
                .all()
            )
            for a in arts:
                if a.filename:
                    out.append(_artifact_to_dict(a, path))
            children = (
                self.db.query(UserFolder.folder_id, UserFolder.name)
                .filter(
                    UserFolder.user_id == user_id,
                    UserFolder.parent_folder_id == fid,
                    UserFolder.deleted_at.is_(None),
                )
                .all()
            )
            for child_id, child_name in children:
                sub = (path + "/" + child_name) if path else child_name
                stack.append((child_id, sub))
        return out

    def _list_team_subtree(self, team_id: str, root_id: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        stack: List[Tuple[str, str]] = [(root_id, "")]
        guard = 0
        while stack and guard < 5000:
            guard += 1
            fid, path = stack.pop()
            arts = (
                self.db.query(Artifact)
                .filter(
                    Artifact.team_id == team_id,
                    Artifact.team_folder_id == fid,
                    Artifact.deleted_at.is_(None),
                )
                .order_by(Artifact.created_at.desc())
                .all()
            )
            for a in arts:
                if a.filename:
                    out.append(_artifact_to_dict(a, path))
            children = (
                self.db.query(TeamFolder.folder_id, TeamFolder.name)
                .filter(
                    TeamFolder.team_id == team_id,
                    TeamFolder.parent_folder_id == fid,
                    TeamFolder.deleted_at.is_(None),
                )
                .all()
            )
            for child_id, child_name in children:
                sub = (path + "/" + child_name) if path else child_name
                stack.append((child_id, sub))
        return out

    # ── Capacity (byte total accumulated over the whole linked-folder subtree) ──
    def capacity_used(self, project: Project) -> int:
        if project.kind == "personal":
            if not project.linked_folder_id:
                return 0
            ids = self._user_subtree_ids(project.owner_user_id, project.linked_folder_id)
            if not ids:
                return 0
            total = (
                self.db.query(func.coalesce(func.sum(Artifact.size_bytes), 0))
                .filter(
                    Artifact.user_id == project.owner_user_id,
                    Artifact.team_id.is_(None),
                    Artifact.user_folder_id.in_(ids),
                    Artifact.deleted_at.is_(None),
                )
                .scalar()
            )
            return int(total or 0)
        if project.kind == "team":
            if not project.linked_team_folder_id or not project.team_id:
                return 0
            ids = self._team_subtree_ids(project.team_id, project.linked_team_folder_id)
            if not ids:
                return 0
            total = (
                self.db.query(func.coalesce(func.sum(Artifact.size_bytes), 0))
                .filter(
                    Artifact.team_id == project.team_id,
                    Artifact.team_folder_id.in_(ids),
                    Artifact.deleted_at.is_(None),
                )
                .scalar()
            )
            return int(total or 0)
        return 0

    def capacity_limit(self) -> int:
        return _capacity_limit()

    def _user_subtree_ids(self, user_id: str, root: str) -> List[str]:
        out: List[str] = []
        stack = [root]
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

    def _team_subtree_ids(self, team_id: str, root: str) -> List[str]:
        out: List[str] = []
        stack = [root]
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

    # ── Upload (write under the linked folder, optionally creating subfolders by subpath) ──
    def upload(
        self,
        project: Project,
        user_id: str,
        file_bytes: bytes,
        filename: str,
        mime_type: Optional[str],
    ) -> Dict[str, Any]:
        """Write bytes directly to the linked folder (or the subfolder resolved from the
        ``filename`` path).

        When ``filename`` looks like ``"q1.xlsx"``, write to the linked folder root;
        when ``"folder/sub/file.ext"``, mkdir the subfolders as needed and then write
        the file.
        """
        from fastapi import HTTPException

        if not filename:
            raise HTTPException(status_code=400, detail="文件名不能为空")
        if not file_bytes:
            raise HTTPException(status_code=400, detail="文件内容为空")
        if len(file_bytes) > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="单文件最大 50 MB")

        used = self.capacity_used(project)
        limit = self.capacity_limit()
        if used + len(file_bytes) > limit:
            raise HTTPException(
                status_code=400,
                detail=f"项目容量不足（已用 {used} / 上限 {limit} 字节）",
            )

        # Split the filename path
        rel = filename.replace("\\", "/").strip("/")
        parts = [p for p in rel.split("/") if p and p not in (".", "..")]
        if not parts:
            raise HTTPException(status_code=400, detail="文件名不合法")
        leaf = parts[-1]
        dirs = parts[:-1]

        if project.kind == "personal":
            target_folder_id = self._ensure_user_subfolder_chain(
                project.owner_user_id, project.linked_folder_id, dirs, actor=user_id
            )
        else:
            target_folder_id = self._ensure_team_subfolder_chain(
                project.team_id, project.linked_team_folder_id, dirs, actor=user_id
            )

        env = os.getenv("ENVIRONMENT", "dev")
        artifact_id = f"pj_{uuid.uuid4().hex[:16]}"
        owner_user_id = (
            project.owner_user_id if project.kind == "personal" else user_id
        )
        storage_prefix = (
            f"{env}/{owner_user_id}/user_uploads/{artifact_id}/{leaf}"
            if project.kind == "personal"
            else f"{env}/teams/{project.team_id}/{artifact_id}/{leaf}"
        )
        try:
            storage_url = get_storage().upload_bytes(file_bytes, storage_prefix)
        except Exception as exc:
            logger.warning("[project_file] upload_bytes 失败 key=%s: %s", storage_prefix, exc)
            raise HTTPException(status_code=500, detail=f"文件上传失败: {exc}")

        artifact = Artifact(
            artifact_id=artifact_id,
            user_id=owner_user_id,
            team_id=project.team_id if project.kind == "team" else None,
            team_folder_id=target_folder_id if project.kind == "team" else None,
            user_folder_id=target_folder_id if project.kind == "personal" else None,
            type="other",
            title=leaf,
            filename=leaf,
            size_bytes=len(file_bytes),
            mime_type=mime_type or "application/octet-stream",
            storage_key=storage_prefix,
            storage_url=storage_url,
            extra_data={
                "source": "user_upload",
                "via_project": project.project_id,
            },
        )
        self.db.add(artifact)
        self.db.commit()
        self.db.refresh(artifact)

        self.audit_repo.create({
            "user_id": user_id,
            "action": "project_file.uploaded",
            "resource_type": "project",
            "resource_id": project.project_id,
            "details": {
                "artifact_id": artifact_id,
                "filename": filename,
                "size": len(file_bytes),
                "target_folder_id": target_folder_id,
            },
            "status": "success",
        })

        folder_path = "/".join(dirs)
        return _artifact_to_dict(artifact, folder_path)

    def _ensure_user_subfolder_chain(
        self,
        user_id: str,
        root_folder_id: Optional[str],
        names: List[str],
        *,
        actor: str,
    ) -> Optional[str]:
        from core.services.user_folder_service import UserFolderService

        svc = UserFolderService(self.db)
        parent_id = root_folder_id
        for name in names:
            existing = (
                self.db.query(UserFolder)
                .filter(
                    UserFolder.user_id == user_id,
                    UserFolder.parent_folder_id == parent_id,
                    UserFolder.name == name,
                    UserFolder.deleted_at.is_(None),
                )
                .first()
            )
            if existing is not None:
                parent_id = existing.folder_id
                continue
            res = svc.create_folder(
                user_id=user_id,
                parent_folder_id=parent_id,
                name=name,
                actor=actor,
            )
            if not res.ok or not res.folder_id:
                from fastapi import HTTPException
                raise HTTPException(status_code=400, detail=res.message or "新建子文件夹失败")
            parent_id = res.folder_id
        return parent_id

    def _ensure_team_subfolder_chain(
        self,
        team_id: str,
        root_folder_id: Optional[str],
        names: List[str],
        *,
        actor: str,
    ) -> Optional[str]:
        try:
            from core.services.team_folder_service import TeamFolderService
        except ModuleNotFoundError:  # CE: no team folders, the team path is unreachable
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="团队功能在当前版本不可用")

        svc = TeamFolderService(self.db)
        parent_id = root_folder_id
        for name in names:
            existing = (
                self.db.query(TeamFolder)
                .filter(
                    TeamFolder.team_id == team_id,
                    TeamFolder.parent_folder_id == parent_id,
                    TeamFolder.name == name,
                    TeamFolder.deleted_at.is_(None),
                )
                .first()
            )
            if existing is not None:
                parent_id = existing.folder_id
                continue
            res = svc.create_folder(
                team_id=team_id,
                parent_folder_id=parent_id,
                name=name,
                actor=actor,
            )
            if not res.ok or not res.folder_id:
                from fastapi import HTTPException
                raise HTTPException(status_code=400, detail=res.message or "新建团队子文件夹失败")
            parent_id = res.folder_id
        return parent_id
