"""Delete the observed identity/version, never a later occupant of the same path."""
from core.infra.time import utc_now


def delete_observed(*, user_id, rel, target):
    from core.db.engine import SessionLocal
    from core.db.models import Artifact, UserFolder, UserShadow
    from core.myspace import mirror
    from core.services.folder_batch import lock_folder_owner
    from core.services.user_folder_service import UserFolderService
    from core.llm.tools.myspace_vfs import myspace_cache_file

    with SessionLocal() as db:
        lock_folder_owner(db, UserShadow, "user_id", user_id)
        if myspace_cache_file(user_id, rel).exists():
            return True  # The path was recreated while approval was pending.
        if target.registered:
            expected = target.registered
            art = db.query(Artifact).filter(
                Artifact.artifact_id == expected.artifact_id,
                Artifact.user_id == user_id,
            ).with_for_update().first()
            if art is None or art.deleted_at is not None:
                return True
            if mirror._artifact_ts(art) != expected.registered_ts:
                return True
            art.deleted_at = utc_now()
            db.commit()
            return True
        folder = db.query(UserFolder).filter(
            UserFolder.folder_id == target.folder_id, UserFolder.user_id == user_id,
        ).with_for_update().first()
        if folder is None or folder.deleted_at is not None:
            return True
        if mirror._ts(folder.updated_at or folder.created_at) != target.folder_ts:
            return True
        result, _ = UserFolderService(db).delete_folder(folder.folder_id, user_id)
        return result.ok
