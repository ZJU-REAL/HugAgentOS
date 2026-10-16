"""Identity-preserving personal-space metadata operations; never mutate the already changed disk."""

from core.db.models import Artifact, UserFolder, UserShadow
from core.services.artifact_edition import personal_artifact_predicates
from core.services.folder_batch import lock_folder_owner

from .metadata import Domain


def domain(user_id):
    return Domain(
        UserFolder,
        "user_id",
        user_id,
        "user_folder_id",
        (Artifact.user_id == user_id, *personal_artifact_predicates(Artifact)),
        user_id,
    )


def apply(user_id, change):
    from core.db.engine import SessionLocal
    from core.llm.tools.myspace_vfs import myspace_cache_file
    from core.services.project_source import reserve_sqlite_writer

    if change.kind == "moved" and change.destination:
        from .personal_move import apply_move

        return apply_move(user_id, change)

    with SessionLocal() as db:
        reserve_sqlite_writer(db)
        lock_folder_owner(db, UserShadow, "user_id", user_id)
        scope = domain(user_id)
        from .content import artifact_timestamp, signature
        from .index import remember, snapshot

        state = snapshot(user_id)
        if change.directory and change.kind == "created":
            if not myspace_cache_file(user_id, change.source).is_dir():
                return False
            baseline = state.get(change.source)
            if baseline and baseline.get("directory"):
                previous = db.get(UserFolder, baseline.get("id"))
                if (
                    previous is not None
                    and (
                        previous.deleted_at is not None
                        or scope.folders(db).get(change.source) is not previous
                    )
                    and (baseline.get("signature") or [])[:2]
                    == signature(myspace_cache_file(user_id, change.source))[:2]
                ):
                    return False  # Ignore late creations after a cloud deletion or rename.
            fid = scope.directory(db, change.source)
            db.commit()
            remember(
                user_id,
                change.source,
                {
                    "id": fid,
                    "directory": True,
                    "signature": signature(myspace_cache_file(user_id, change.source)),
                    "version": artifact_timestamp(db.get(UserFolder, fid)),
                },
            )
            return True
        return False


def restore_move(user_id, change):
    """A declined approval restores the already moved bytes before other events run."""
    import os

    from core.sandbox._common import myspace_cache_dir

    from .files import parent_fd

    root = myspace_cache_dir(user_id)
    with parent_fd(root, change.source, create=True) as (source_parent, source_leaf):
        with parent_fd(root, change.destination) as (dest_parent, dest_leaf):
            try:
                os.stat(source_leaf, dir_fd=source_parent, follow_symlinks=False)
            except FileNotFoundError:
                os.replace(dest_leaf, source_leaf, src_dir_fd=dest_parent, dst_dir_fd=source_parent)
            else:
                raise RuntimeError("Cannot restore declined move over a new source")
