"""Personal file transfers; folder tree operations live in UserFolderService."""
from __future__ import annotations
import os
import uuid
from typing import Optional
from core.infra.time import utc_now
from core.db.models import Artifact
from core.db.personal_file_names import check_name
from core.db.repository import ArtifactRepository
from core.services.artifact_edition import is_personal_artifact, personal_artifact_create_fields

class PersonalFileOperations:
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
        from core.services.user_folder_service import FolderResult

        artifact = (
            self.db.query(Artifact)
            .filter(Artifact.artifact_id == artifact_id, Artifact.deleted_at.is_(None))
            .populate_existing().with_for_update()
            .first()
        )
        if artifact is None or artifact.user_id != actor:
            return FolderResult(False, "文件不存在")
        if not is_personal_artifact(artifact):
            return FolderResult(False, "非个人文件不能在个人空间中移动")

        if target_folder_id is not None:
            target = self.get(target_folder_id, lock=True)
            if target is None or target.user_id != actor:
                return FolderResult(False, "目标文件夹不存在")

        check_name(self.db, actor, target_folder_id, artifact.filename, exclude_id=artifact_id)
        old_folder = self.get(artifact.user_folder_id) if artifact.user_folder_id else None
        old_rel = f"{self._folder_rel(old_folder)}/" if old_folder is not None else ""
        old_rel += str(artifact.filename or "")
        artifact.user_folder_id = target_folder_id
        artifact.updated_at = utc_now()
        self.db.commit()
        self._drop_mirror(str(artifact.user_id), old_rel, is_dir=False)

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
        from core.services.user_folder_service import FolderResult

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

        check_name(self.db, actor, target_folder_id, artifact.filename)
        from core.storage import get_storage

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
