"""Personal project directory bindings and file operations."""

from __future__ import annotations
from core.infra.time import utc_now

import logging
import os
import uuid
from core.db.models import UserShadow
from core.db.repository.folder import lock_folder_owner
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.db.models import UserFolder
from core.db.repository import AuditLogRepository
from core.files.storage import get_storage
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)



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
    if "/" in cleaned or "\\" in cleaned or any(ord(c) < 32 for c in cleaned) or cleaned in (".", ".."):
        return False, "文件夹名称非法（不能包含 / 或仅为 . / ..）"
    return True, cleaned


from core.files.personal_operations import PersonalFileOperations


class UserFolderService(PersonalFileOperations):
    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditLogRepository(db)

    # ── Queries ──────────────────────────────────────────
    def get(self, folder_id: str, *, lock: bool = False) -> Optional[UserFolder]:
        query = self.db.query(UserFolder).filter(
            UserFolder.folder_id == folder_id, UserFolder.deleted_at.is_(None),
        )
        if lock:
            query = query.populate_existing().with_for_update()
        return query.first()

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
        nodes = {f.folder_id: {"folder_id": f.folder_id, "user_id": f.user_id, "parent_folder_id": f.parent_folder_id, "name": f.name,
                  "created_at": f.created_at.isoformat() if f.created_at else None,
                  "children": []} for f in all_folders}
        roots = []
        for f in all_folders:
            node = nodes[f.folder_id]
            if f.parent_folder_id is None:
                roots.append(node)
            elif f.parent_folder_id in nodes:
                nodes[f.parent_folder_id]["children"].append(node)
        return roots

    def get_breadcrumb(self, folder_id: str, user_id: str) -> List[Dict[str, Any]]:
        """Return the path from the root to the current folder (excluding the "My Space" root node)."""
        chain: List[UserFolder] = []
        cur_id: Optional[str] = folder_id
        seen = set()
        while cur_id and cur_id not in seen:
            seen.add(cur_id)
            folder = self.get(cur_id)
            if folder is None or folder.user_id != user_id:
                break
            chain.append(folder)
            cur_id = folder.parent_folder_id
        chain.reverse()
        return [
            {"folder_id": f.folder_id, "name": f.name, "parent_folder_id": f.parent_folder_id}
            for f in chain
        ]


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

        lock_folder_owner(self.db, UserShadow, "user_id", user_id)

        if parent_folder_id is not None:
            parent = self.get(parent_folder_id, lock=True)
            if parent is None or parent.user_id != user_id:
                return FolderResult(False, "父文件夹不存在或不属于该用户")

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
        from core.files.spaces.files import SpaceFiles
        if actor != user_id:
            return FolderResult(False, "无权创建该用户的文件夹")
        path = self._folder_rel(folder)
        files = SpaceFiles(self.db, actor, get_storage())
        info = files.storage.stat_object(files.grant("personal").key(path))
        if info is None:
            files.mkdir("personal", path)
        elif not info.is_directory:
            from core.db.personal_file_names import conflict
            raise conflict()
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


    def _folder_rel(self, folder: UserFolder) -> str:
        """文件夹相对用户根目录的路径（用当前 DB 状态算，调用方须在改动前调用）。"""
        names = [folder.name]
        cur = folder.parent_folder_id
        seen: set = set()
        while cur is not None and cur not in seen:
            seen.add(cur)
            row = self.get(cur)
            if row is None:
                break
            names.append(row.name)
            cur = row.parent_folder_id
        return "/".join(reversed(names))

    def rename_folder(self, folder_id: str, name: str, actor: str) -> FolderResult:
        lock_folder_owner(self.db, UserShadow, "user_id", actor)
        folder = self.get(folder_id, lock=True)
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

        old_rel = self._folder_rel(folder)
        parent_path = old_rel.rpartition("/")[0]
        new_rel = parent_path + "/" + cleaned if parent_path else cleaned
        from core.files.spaces.files import SpaceFiles
        SpaceFiles(self.db, actor, get_storage()).move("personal", old_rel, new_rel)
        folder.name = cleaned
        folder.updated_at = utc_now()
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
        lock_folder_owner(self.db, UserShadow, "user_id", actor)
        folder = self.get(folder_id, lock=True)
        if folder is None or folder.user_id != actor:
            return FolderResult(False, "文件夹不存在")
        if new_parent_id == folder.parent_folder_id:
            return FolderResult(True, "未改动", folder_id=folder_id)

        if new_parent_id is not None:
            parent = self.get(new_parent_id, lock=True)
            if parent is None or parent.user_id != folder.user_id:
                return FolderResult(False, "目标父文件夹不存在或跨用户")
            # Cycle detection: the target parent must not be a descendant of itself
            cur_id: Optional[str] = new_parent_id
            seen = set()
            while cur_id:
                if cur_id in seen:
                    return FolderResult(False, "文件夹层级非法")
                seen.add(cur_id)
                if cur_id == folder_id:
                    return FolderResult(False, "不能移动到自身或其子孙")
                cur = self.get(cur_id)
                if cur is None:
                    break
                cur_id = cur.parent_folder_id

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

        old_rel = self._folder_rel(folder)
        parent_path = self._folder_rel(parent) if new_parent_id else ""
        new_rel = parent_path + "/" + folder.name if parent_path else folder.name
        from core.files.spaces.files import SpaceFiles
        SpaceFiles(self.db, actor, get_storage()).move("personal", old_rel, new_rel)
        folder.parent_folder_id = new_parent_id
        folder.updated_at = utc_now()
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
        """Delete directory content, then remove its project bindings."""
        lock_folder_owner(self.db, UserShadow, "user_id", actor)
        folder = self.get(folder_id, lock=True)
        if folder is None or folder.user_id != actor:
            return FolderResult(False, "文件夹不存在"), 0

        ids_to_delete = self._collect_descendants(folder_id)
        now = utc_now()

        from core.files.spaces.files import SpaceFiles
        affected = SpaceFiles(self.db, actor, get_storage()).delete_tree("personal", self._folder_rel(folder))

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
        from core.files.spaces.access import namespace
        from core.files.spaces.tree import tree_entries
        folder = self.get_owned(folder_id, user_id)
        if folder is None:
            return 0
        prefix = namespace("users", user_id) + self._folder_rel(folder) + "/"
        return sum(not item.is_directory for item in tree_entries(get_storage(), prefix))
