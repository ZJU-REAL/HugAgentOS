"""Personal project-file service for Community Edition."""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

from core.db.models import Artifact, Project, UserFolder
from core.storage import get_storage
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_DEFAULT_PROJECT_CAPACITY_BYTES = 200 * 1024 * 1024


def _capacity_limit() -> int:
    try:
        return max(int(os.getenv("PROJECT_FILE_CAPACITY_BYTES", "")), 1)
    except (TypeError, ValueError):
        return _DEFAULT_PROJECT_CAPACITY_BYTES


def _artifact_to_dict(artifact: Artifact, folder_path: str) -> Dict[str, Any]:
    return {
        "id": artifact.artifact_id,
        "artifact_id": artifact.artifact_id,
        "name": f"{folder_path}/{artifact.filename}" if folder_path else artifact.filename,
        "title": artifact.title,
        "mime_type": artifact.mime_type,
        "size_bytes": int(artifact.size_bytes or 0),
        "download_url": f"/files/{artifact.artifact_id}",
        "type": artifact.type,
        "folder_path": folder_path,
        "created_at": (
            (artifact.updated_at or artifact.created_at).isoformat()
            if artifact.updated_at or artifact.created_at
            else None
        ),
    }


class ProjectFileService:
    def __init__(self, db: Session):
        self.db = db

    def _user_subtree_ids(self, user_id: str, root: str) -> List[str]:
        out: List[str] = []
        stack = [root]
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

    def list_files(self, project: Project) -> List[Dict[str, Any]]:
        if project.kind != "personal" or not project.linked_folder_id:
            return []
        out: List[Dict[str, Any]] = []
        stack: List[Tuple[str, str]] = [(project.linked_folder_id, "")]
        while stack:
            folder_id, path = stack.pop()
            artifacts = (
                self.db.query(Artifact)
                .filter(
                    Artifact.user_id == project.owner_user_id,
                    Artifact.user_folder_id == folder_id,
                    Artifact.deleted_at.is_(None),
                )
                .order_by(Artifact.created_at.desc())
                .all()
            )
            out.extend(_artifact_to_dict(item, path) for item in artifacts if item.filename)
            children = (
                self.db.query(UserFolder.folder_id, UserFolder.name)
                .filter(
                    UserFolder.user_id == project.owner_user_id,
                    UserFolder.parent_folder_id == folder_id,
                    UserFolder.deleted_at.is_(None),
                )
                .all()
            )
            for child_id, child_name in children:
                stack.append((child_id, f"{path}/{child_name}" if path else child_name))
        return out

    def capacity_used(self, project: Project) -> int:
        if project.kind != "personal" or not project.linked_folder_id:
            return 0
        folder_ids = self._user_subtree_ids(project.owner_user_id, project.linked_folder_id)
        return int(
            self.db.query(func.coalesce(func.sum(Artifact.size_bytes), 0))
            .filter(
                Artifact.user_id == project.owner_user_id,
                Artifact.user_folder_id.in_(folder_ids),
                Artifact.deleted_at.is_(None),
            )
            .scalar()
            or 0
        )

    def capacity_limit(self) -> int:
        return _capacity_limit()

    def _ensure_user_subfolder_chain(
        self,
        user_id: str,
        root_folder_id: Optional[str],
        names: List[str],
        *,
        actor: str,
    ) -> Optional[str]:
        from core.services.user_folder_service import UserFolderService

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
            result = UserFolderService(self.db).create_folder(
                user_id=user_id, parent_folder_id=parent_id, name=name, actor=actor
            )
            if not result.ok or not result.folder_id:
                raise HTTPException(status_code=400, detail=result.message or "新建子文件夹失败")
            parent_id = result.folder_id
        return parent_id

    def upload(
        self,
        project: Project,
        user_id: str,
        file_bytes: bytes,
        filename: str,
        mime_type: Optional[str],
    ) -> Dict[str, Any]:
        if project.kind != "personal":
            raise HTTPException(status_code=404, detail="项目不存在")
        if not filename or not file_bytes:
            raise HTTPException(status_code=400, detail="文件名或内容为空")
        if len(file_bytes) > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="单文件最大 50 MB")
        if self.capacity_used(project) + len(file_bytes) > self.capacity_limit():
            raise HTTPException(status_code=400, detail="项目容量不足")
        parts = [
            part
            for part in filename.replace("\\", "/").strip("/").split("/")
            if part not in ("", ".", "..")
        ]
        if not parts:
            raise HTTPException(status_code=400, detail="文件名不合法")
        leaf, directories = parts[-1], parts[:-1]
        folder_id = self._ensure_user_subfolder_chain(
            project.owner_user_id,
            project.linked_folder_id,
            directories,
            actor=user_id,
        )
        artifact_id = f"pj_{uuid.uuid4().hex[:16]}"
        storage_key = f"{os.getenv('ENVIRONMENT', 'dev')}/{project.owner_user_id}/user_uploads/{artifact_id}/{leaf}"
        storage_url = get_storage().upload_bytes(file_bytes, storage_key)
        artifact = Artifact(
            artifact_id=artifact_id,
            user_id=project.owner_user_id,
            user_folder_id=folder_id,
            type="other",
            title=leaf,
            filename=leaf,
            size_bytes=len(file_bytes),
            mime_type=mime_type or "application/octet-stream",
            storage_key=storage_key,
            storage_url=storage_url,
            extra_data={"source": "user_upload", "via_project": project.project_id},
        )
        self.db.add(artifact)
        self.db.commit()
        self.db.refresh(artifact)
        return _artifact_to_dict(artifact, "/".join(directories))


__all__ = ["ProjectFileService"]
