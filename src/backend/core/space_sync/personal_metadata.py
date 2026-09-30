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

    with SessionLocal() as db:
        reserve_sqlite_writer(db)
        lock_folder_owner(db, UserShadow, "user_id", user_id)
        scope = domain(user_id)
        from .content import artifact_timestamp, signature
        from .index import move, remember, snapshot

        state = snapshot(user_id)
        if change.kind == "moved" and change.destination:
            # The source must remain absent, otherwise this is a stale rename event.
            if myspace_cache_file(user_id, change.source).exists():
                return False
            source = change.source
            destination = myspace_cache_file(user_id, change.destination)
            if not destination.exists():
                raise RuntimeError("Move destination changed; waiting for reconciliation")
            existing = (
                scope.folders(db).get(source) if change.directory else scope.artifact(db, source)
            )
            if existing is None:
                identity = signature(destination)[:2]
                matches = [
                    p
                    for p, s in state.items()
                    if s.get("directory") == change.directory
                    and (s.get("signature") or [])[:2] == identity
                    and not myspace_cache_file(user_id, p).exists()
                ]
                if len(matches) == 1:
                    source = matches[0]
            existing = (
                scope.folders(db).get(source) if change.directory else scope.artifact(db, source)
            )
            baseline = state.get(source)
            if existing and baseline:
                identity = existing.folder_id if change.directory else existing.artifact_id
                if identity != baseline.get("id") or (
                    not change.directory and existing.storage_key != baseline.get("key")
                ):
                    raise RuntimeError("Cloud identity changed; local move retained")
                if baseline.get("version") is not None and baseline[
                    "version"
                ] != artifact_timestamp(existing):
                    if change.directory:
                        raise RuntimeError("Cloud directory version changed; local move retained")
                    import hashlib

                    from core.storage import get_storage

                    if hashlib.sha256(
                        get_storage().download_bytes(existing.storage_key)
                    ).hexdigest() != baseline.get("sha256"):
                        raise RuntimeError("Cloud content version changed; local move retained")
            target = (
                scope.folders(db).get(change.destination)
                if change.directory
                else scope.artifact(db, change.destination)
            )
            replay = (
                target is not None
                and baseline
                and baseline.get("id")
                == getattr(target, "folder_id" if change.directory else "artifact_id")
            )
            row = target if replay else scope.move(db, source, change.destination, change.directory)
            db.commit()
            if row is not None:
                move(user_id, source, change.destination, change.directory)
                restored = snapshot(user_id).get(change.destination)
                if restored:
                    restored["version"] = artifact_timestamp(row)
                    if change.directory:
                        restored["signature"] = signature(destination)
                    remember(user_id, change.destination, restored)
            return row is not None
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
