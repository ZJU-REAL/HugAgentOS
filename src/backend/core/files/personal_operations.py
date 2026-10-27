"""Save personal conversation attachments into mounted directories."""
from core.db.models import Artifact
from core.infra.time import utc_now
from core.db.artifact_scope import is_personal_artifact
from core.files.spaces.files import SpaceFiles
from core.files.storage import get_storage


class PersonalFileOperations:
    def _save_artifact(self, artifact_id, target_folder_id, actor, *, move):
        from core.projects.user_folders import FolderResult
        artifact = self.db.query(Artifact).filter(
            Artifact.artifact_id == artifact_id, Artifact.deleted_at.is_(None),
        ).first()
        if artifact is None or artifact.user_id != actor or not is_personal_artifact(artifact):
            return FolderResult(False, "文件不存在")
        target = self.get_owned(target_folder_id, actor) if target_folder_id else None
        if target_folder_id and target is None:
            return FolderResult(False, "目标文件夹不存在")
        name = artifact.filename
        from core.files.storage.entries import validate_key
        try:
            validate_key(name)
            if "/" in name:
                raise ValueError("文件名无效")
        except (TypeError, ValueError):
            return FolderResult(False, "文件名无效")
        path = "/".join(filter(None, [self._folder_rel(target) if target else "", name]))
        storage = get_storage()
        files = SpaceFiles(self.db, actor, storage)
        data = storage.download_bytes(artifact.storage_key)
        # Uploads are already mirrored into My Space, so saving one onto its own copy is a no-op.
        files.create("personal", path, data, idempotent=True)
        if move:
            artifact.deleted_at = utc_now()
            self.db.commit()
        return FolderResult(True, "已保存到文件夹", artifact_id=path)

    def move_artifact(self, artifact_id, target_folder_id, actor):
        return self._save_artifact(artifact_id, target_folder_id, actor, move=True)

    def copy_artifact(self, artifact_id, target_folder_id, actor):
        return self._save_artifact(artifact_id, target_folder_id, actor, move=False)
