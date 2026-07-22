"""Personal folder (UserFolder) business logic.

Manages only the "My Space" personal file hierarchy:
- depth limit (MAX_FOLDER_DEPTH)
- same-name check among siblings (with soft-delete fallback)
- cycle detection on move
- cascading soft delete (itself + descendant folders + associated personal artifacts)
- audit persistence

Personal artifact ownership is determined by ``user_id`` and
``user_folder_id``. Edition-specific ownership transitions are handled outside
this shared service.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.db.models import Artifact, UserFolder
from core.db.repository import ArtifactRepository, AuditLogRepository
from core.services.artifact_edition import is_personal_artifact, personal_artifact_create_fields
from core.storage import get_storage
from sqlalchemy.orm import Session

MAX_FOLDER_DEPTH = 8


@dataclass
class FolderResult:
    ok: bool
    message: str
    folder_id: Optional[str] = None
    artifact_id: Optional[str] = None  # copy_artifact returns the id of the newly created copy


def _sanitize_name(name: str) -> Tuple[bool, str]:
    cleaned = (name or "").strip()
    if not cleaned:
        return False, "文件夹名称不能为空"
    if len(cleaned) > 255:
        return False, "文件夹名称过长"
    if "/" in cleaned or cleaned in (".", ".."):
        return False, "文件夹名称非法（不能包含 / 或仅为 . / ..）"
    return True, cleaned


class UserFolderService:
    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditLogRepository(db)

    # ── Queries ──────────────────────────────────────────
    def get(self, folder_id: str) -> Optional[UserFolder]:
        return (
            self.db.query(UserFolder)
            .filter(UserFolder.folder_id == folder_id, UserFolder.deleted_at.is_(None))
            .first()
        )

    def get_owned(self, folder_id: str, user_id: str) -> Optional[UserFolder]:
        """get with ownership check: must be the current user's folder, otherwise treated as non-existent (403/404 equivalent)."""
        folder = self.get(folder_id)
        if folder is None or folder.user_id != user_id:
            return None
        return folder

    def list_by_user(self, user_id: str) -> List[UserFolder]:
        return (
            self.db.query(UserFolder)
            .filter(UserFolder.user_id == user_id, UserFolder.deleted_at.is_(None))
            .order_by(UserFolder.parent_folder_id.nullsfirst(), UserFolder.name)
            .all()
        )

    def get_tree(self, user_id: str) -> List[Dict[str, Any]]:
        """Return the user's full personal folder tree as nested dicts (NULL parent = top level)."""
        all_folders = self.list_by_user(user_id)
        children_map: Dict[Optional[str], List[UserFolder]] = {}
        for f in all_folders:
            children_map.setdefault(f.parent_folder_id, []).append(f)

        def serialize(folder: UserFolder) -> Dict[str, Any]:
            return {
                "folder_id": folder.folder_id,
                "user_id": folder.user_id,
                "parent_folder_id": folder.parent_folder_id,
                "name": folder.name,
                "created_at": folder.created_at.isoformat() if folder.created_at else None,
                "children": [serialize(c) for c in children_map.get(folder.folder_id, [])],
            }

        return [serialize(root) for root in children_map.get(None, [])]

    def get_breadcrumb(self, folder_id: str, user_id: str) -> List[Dict[str, Any]]:
        """Return the path from the root to the current folder (excluding the "My Space" root node)."""
        chain: List[UserFolder] = []
        cur_id: Optional[str] = folder_id
        guard = 0
        while cur_id and guard < MAX_FOLDER_DEPTH + 2:
            folder = self.get(cur_id)
            if folder is None or folder.user_id != user_id:
                break
            chain.append(folder)
            cur_id = folder.parent_folder_id
            guard += 1
        chain.reverse()
        return [
            {"folder_id": f.folder_id, "name": f.name, "parent_folder_id": f.parent_folder_id}
            for f in chain
        ]

    def _depth(self, parent_id: Optional[str]) -> int:
        depth = 0
        cur_id = parent_id
        while cur_id:
            folder = self.get(cur_id)
            if folder is None:
                break
            depth += 1
            cur_id = folder.parent_folder_id
            if depth > MAX_FOLDER_DEPTH + 1:
                break
        return depth

    # ── Write operations ─────────────────────────────────
    def create_folder(
        self,
        user_id: str,
        parent_folder_id: Optional[str],
        name: str,
        actor: str,
    ) -> FolderResult:
        ok, cleaned = _sanitize_name(name)
        if not ok:
            return FolderResult(False, cleaned)

        if parent_folder_id is not None:
            parent = self.get(parent_folder_id)
            if parent is None or parent.user_id != user_id:
                return FolderResult(False, "父文件夹不存在或不属于该用户")
            if self._depth(parent_folder_id) >= MAX_FOLDER_DEPTH:
                return FolderResult(False, f"层级超出上限（≤{MAX_FOLDER_DEPTH} 级）")

        duplicate = (
            self.db.query(UserFolder)
            .filter(
                UserFolder.user_id == user_id,
                (
                    UserFolder.parent_folder_id.is_(parent_folder_id)
                    if parent_folder_id is None
                    else UserFolder.parent_folder_id == parent_folder_id
                ),
                UserFolder.name == cleaned,
                UserFolder.deleted_at.is_(None),
            )
            .first()
        )
        if duplicate:
            return FolderResult(False, "同级已有同名文件夹")

        folder_id = f"ufld_{uuid.uuid4().hex[:16]}"
        folder = UserFolder(
            folder_id=folder_id,
            user_id=user_id,
            parent_folder_id=parent_folder_id,
            name=cleaned,
        )
        self.db.add(folder)
        self.db.commit()

        self.audit.create(
            {
                "user_id": actor,
                "action": "user_folder.create",
                "resource_type": "user_folder",
                "resource_id": folder_id,
                "details": {"user_id": user_id, "parent": parent_folder_id, "name": cleaned},
                "status": "success",
            }
        )
        return FolderResult(True, "文件夹已创建", folder_id=folder_id)

    def rename_folder(self, folder_id: str, name: str, actor: str) -> FolderResult:
        folder = self.get(folder_id)
        if folder is None or folder.user_id != actor:
            return FolderResult(False, "文件夹不存在")

        ok, cleaned = _sanitize_name(name)
        if not ok:
            return FolderResult(False, cleaned)
        if cleaned == folder.name:
            return FolderResult(True, "文件夹未变更", folder_id=folder_id)

        duplicate = (
            self.db.query(UserFolder)
            .filter(
                UserFolder.user_id == folder.user_id,
                (
                    UserFolder.parent_folder_id.is_(folder.parent_folder_id)
                    if folder.parent_folder_id is None
                    else UserFolder.parent_folder_id == folder.parent_folder_id
                ),
                UserFolder.name == cleaned,
                UserFolder.deleted_at.is_(None),
                UserFolder.folder_id != folder_id,
            )
            .first()
        )
        if duplicate:
            return FolderResult(False, "同级已有同名文件夹")

        folder.name = cleaned
        folder.updated_at = datetime.utcnow()
        self.db.commit()

        self.audit.create(
            {
                "user_id": actor,
                "action": "user_folder.rename",
                "resource_type": "user_folder",
                "resource_id": folder_id,
                "details": {"name": cleaned},
                "status": "success",
            }
        )
        return FolderResult(True, "已重命名", folder_id=folder_id)

    def move_folder(
        self,
        folder_id: str,
        new_parent_id: Optional[str],
        actor: str,
    ) -> FolderResult:
        folder = self.get(folder_id)
        if folder is None or folder.user_id != actor:
            return FolderResult(False, "文件夹不存在")
        if new_parent_id == folder.parent_folder_id:
            return FolderResult(True, "未改动", folder_id=folder_id)

        if new_parent_id is not None:
            parent = self.get(new_parent_id)
            if parent is None or parent.user_id != folder.user_id:
                return FolderResult(False, "目标父文件夹不存在或跨用户")
            # Cycle detection: the target parent must not be a descendant of itself
            cur_id: Optional[str] = new_parent_id
            while cur_id:
                if cur_id == folder_id:
                    return FolderResult(False, "不能移动到自身或其子孙")
                cur = self.get(cur_id)
                if cur is None:
                    break
                cur_id = cur.parent_folder_id
            if self._depth(new_parent_id) >= MAX_FOLDER_DEPTH:
                return FolderResult(False, f"层级超出上限（≤{MAX_FOLDER_DEPTH} 级）")

        # Sibling name check: after the move it must not collide with a name under the new parent
        duplicate = (
            self.db.query(UserFolder)
            .filter(
                UserFolder.user_id == folder.user_id,
                (
                    UserFolder.parent_folder_id.is_(new_parent_id)
                    if new_parent_id is None
                    else UserFolder.parent_folder_id == new_parent_id
                ),
                UserFolder.name == folder.name,
                UserFolder.deleted_at.is_(None),
                UserFolder.folder_id != folder_id,
            )
            .first()
        )
        if duplicate:
            return FolderResult(False, "目标位置已有同名文件夹")

        folder.parent_folder_id = new_parent_id
        folder.updated_at = datetime.utcnow()
        self.db.commit()

        self.audit.create(
            {
                "user_id": actor,
                "action": "user_folder.move",
                "resource_type": "user_folder",
                "resource_id": folder_id,
                "details": {"new_parent_id": new_parent_id},
                "status": "success",
            }
        )
        return FolderResult(True, "已移动", folder_id=folder_id)

    def _collect_descendants(self, folder_id: str) -> List[str]:
        """Collect the folder's own id + all descendant folder ids (active, non-deleted nodes)."""
        ids: List[str] = [folder_id]
        stack = [folder_id]
        while stack:
            cur = stack.pop()
            children = (
                self.db.query(UserFolder.folder_id)
                .filter(
                    UserFolder.parent_folder_id == cur,
                    UserFolder.deleted_at.is_(None),
                )
                .all()
            )
            for (cid,) in children:
                ids.append(cid)
                stack.append(cid)
        return ids

    def delete_folder(self, folder_id: str, actor: str) -> Tuple[FolderResult, int]:
        """Cascading soft delete of the folder plus all descendant folders and associated artifacts. Returns the number of affected files."""
        folder = self.get(folder_id)
        if folder is None or folder.user_id != actor:
            return FolderResult(False, "文件夹不存在"), 0

        ids_to_delete = self._collect_descendants(folder_id)
        now = datetime.utcnow()

        affected = (
            self.db.query(Artifact)
            .filter(
                Artifact.user_folder_id.in_(ids_to_delete),
                Artifact.deleted_at.is_(None),
            )
            .update({Artifact.deleted_at: now}, synchronize_session=False)
        )

        self.db.query(UserFolder).filter(
            UserFolder.folder_id.in_(ids_to_delete),
            UserFolder.deleted_at.is_(None),
        ).update({UserFolder.deleted_at: now}, synchronize_session=False)

        self.db.commit()

        self.audit.create(
            {
                "user_id": actor,
                "action": "user_folder.delete",
                "resource_type": "user_folder",
                "resource_id": folder_id,
                "details": {
                    "cascaded_folder_ids": ids_to_delete,
                    "artifacts_affected": int(affected or 0),
                },
                "status": "success",
            }
        )
        return FolderResult(True, "文件夹已删除", folder_id=folder_id), int(affected or 0)

    def count_affected_artifacts(self, folder_id: str, user_id: str) -> int:
        """Preflight: how many files deleting this folder would affect (including all descendant subfolders)."""
        folder = self.get(folder_id)
        if folder is None or folder.user_id != user_id:
            return 0
        ids = self._collect_descendants(folder_id)
        return (
            self.db.query(Artifact)
            .filter(
                Artifact.user_folder_id.in_(ids),
                Artifact.deleted_at.is_(None),
            )
            .count()
        )

    # ── Artifact moves ───────────────────────────────────
    def move_artifact(
        self,
        artifact_id: str,
        target_folder_id: Optional[str],
        actor: str,
    ) -> FolderResult:
        """Move a personal artifact to the given personal folder (None = root).

        Validation:
        - the artifact exists and belongs to the actor
        - the target folder (if any) belongs to the actor
        - the artifact must be a personal file.
        """
        artifact = (
            self.db.query(Artifact)
            .filter(Artifact.artifact_id == artifact_id, Artifact.deleted_at.is_(None))
            .first()
        )
        if artifact is None or artifact.user_id != actor:
            return FolderResult(False, "文件不存在")
        if not is_personal_artifact(artifact):
            return FolderResult(False, "非个人文件不能在个人空间中移动")

        if target_folder_id is not None:
            target = self.get(target_folder_id)
            if target is None or target.user_id != actor:
                return FolderResult(False, "目标文件夹不存在")

        artifact.user_folder_id = target_folder_id
        artifact.updated_at = datetime.utcnow()
        self.db.commit()

        self.audit.create(
            {
                "user_id": actor,
                "action": "user_folder.move_artifact",
                "resource_type": "artifact",
                "resource_id": artifact_id,
                "details": {"target_folder_id": target_folder_id},
                "status": "success",
            }
        )
        return FolderResult(True, "已移动")

    def copy_artifact(
        self,
        artifact_id: str,
        target_folder_id: Optional[str],
        actor: str,
    ) -> FolderResult:
        """**Non-destructively** copy a personal artifact into the given personal folder (None = root).

        Difference from move: move only changes ``user_folder_id`` (a single DB
        field; source/storage untouched); copy creates a separate new artifact
        record + duplicates the storage object (``download_bytes`` from the old key
        → ``upload_bytes`` to a new key), leaving the source file intact. Same
        validation as move: exists, belongs to the actor, is personal, target
        folder belongs to the actor.
        """
        artifact = (
            self.db.query(Artifact)
            .filter(Artifact.artifact_id == artifact_id, Artifact.deleted_at.is_(None))
            .first()
        )
        if artifact is None or artifact.user_id != actor:
            return FolderResult(False, "文件不存在")
        if not is_personal_artifact(artifact):
            return FolderResult(False, "非个人文件不能复制到个人空间")

        if target_folder_id is not None:
            target = self.get(target_folder_id)
            if target is None or target.user_id != actor:
                return FolderResult(False, "目标文件夹不存在")

        storage = get_storage()
        new_id = f"ua_{uuid.uuid4().hex[:16]}"
        env = os.getenv("ENVIRONMENT", "dev")
        new_key = f"{env}/{actor}/user_uploads/{new_id}/{artifact.filename}"
        try:
            content = storage.download_bytes(artifact.storage_key)
            new_url = storage.upload_bytes(content, new_key)
        except (
            Exception
        ) as exc:  # noqa: BLE001 — leave no half-written record when storage I/O fails
            return FolderResult(False, f"复制失败：存储对象读写出错（{exc}）")

        extra = dict(artifact.extra_data or {})
        extra.update({"source": "copy_personal", "copied_from": artifact.artifact_id})
        ArtifactRepository(self.db).create(
            {
                "artifact_id": new_id,
                "chat_id": None,
                "user_id": actor,
                "user_folder_id": target_folder_id,
                **personal_artifact_create_fields(),
                "type": artifact.type,
                "title": artifact.title,
                "filename": artifact.filename,
                "size_bytes": artifact.size_bytes,
                "mime_type": artifact.mime_type,
                "storage_key": new_key,
                "storage_url": new_url,
                "extra_data": extra,
            }
        )

        self.audit.create(
            {
                "user_id": actor,
                "action": "user_folder.copy_artifact",
                "resource_type": "artifact",
                "resource_id": new_id,
                "details": {"target_folder_id": target_folder_id, "copied_from": artifact_id},
                "status": "success",
            }
        )
        return FolderResult(True, "已复制", artifact_id=new_id)
