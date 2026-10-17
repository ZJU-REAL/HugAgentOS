"""Check cloud bytes without a transaction, then atomically publish a personal move."""

import hashlib

from core.db.models import UserShadow
from core.llm.tools.myspace_vfs import myspace_cache_file
from core.services.folder_batch import lock_folder_owner

from .content import _artifact_at, _revision, artifact_timestamp, signature
from .index import move, remember, snapshot


def _at(db, scope, path, directory, *, lock=False):
    if not directory:
        return _artifact_at(db, scope, path, lock=lock)
    row = scope.folders(db).get(path)
    if row is not None and lock:
        model = scope.folder_model
        return (
            db.query(model)
            .filter_by(folder_id=row.folder_id, deleted_at=None)
            .populate_existing()
            .with_for_update(nowait=True)
            .one_or_none()
        )
    return row


def _stamp(row, directory):
    if row is None:
        return None
    if directory:
        return (row.folder_id, row.parent_folder_id, row.name, row.updated_at, row.created_at)
    return _revision(row)


def apply_move(user_id, change):
    from core.db.engine import SessionLocal
    from core.services.project_source import reserve_sqlite_writer
    from core.storage import get_storage
    from .personal_metadata import domain

    if myspace_cache_file(user_id, change.source).exists():
        return False
    destination = myspace_cache_file(user_id, change.destination)
    if not destination.exists():
        raise RuntimeError("Move destination changed; waiting for reconciliation")
    observed = signature(destination)
    scope, state, source = domain(user_id), snapshot(user_id), change.source
    with SessionLocal() as db:
        existing = _at(db, scope, source, change.directory)
        if existing is None:
            matches = [
                path
                for path, saved in state.items()
                if saved.get("directory") == change.directory
                and (saved.get("signature") or [])[:2] == observed[:2]
                and not myspace_cache_file(user_id, path).exists()
            ]
            if len(matches) == 1:
                source = matches[0]
                existing = _at(db, scope, source, change.directory)
        baseline = state.get(source)
        expected = _stamp(existing, change.directory)
        target = _at(db, scope, change.destination, change.directory)
        expected_target = _stamp(target, change.directory)
        version = artifact_timestamp(existing) if existing else None
        identity = (
            (existing.folder_id if change.directory else existing.artifact_id) if existing else None
        )
        storage_key = existing.storage_key if existing and not change.directory else None

    # No row or owner lock, or even a read transaction, spans this network call.
    if existing and baseline:
        if identity != baseline.get("id") or (
            not change.directory and storage_key != baseline.get("key")
        ):
            raise RuntimeError("Cloud identity changed; local move retained")
        if baseline.get("version") is not None and baseline["version"] != version:
            if change.directory:
                raise RuntimeError("Cloud directory version changed; local move retained")
            if hashlib.sha256(
                get_storage().download_bytes(storage_key)
            ).hexdigest() != baseline.get("sha256"):
                raise RuntimeError("Cloud content version changed; local move retained")

    with SessionLocal() as db:
        reserve_sqlite_writer(db)
        lock_folder_owner(db, UserShadow, "user_id", user_id)
        current = _at(db, scope, source, change.directory, lock=True)
        target = _at(db, scope, change.destination, change.directory, lock=True)
        if (
            _stamp(current, change.directory) != expected
            or _stamp(target, change.directory) != expected_target
        ):
            raise RuntimeError("Cloud changed during move verification; local move retained")
        if (
            myspace_cache_file(user_id, change.source).exists()
            or signature(destination) != observed
        ):
            raise RuntimeError("Local move changed during verification")
        replay = (
            target is not None
            and baseline
            and baseline.get("id") == (target.folder_id if change.directory else target.artifact_id)
        )
        row = target if replay else scope.move(db, source, change.destination, change.directory)
        version = artifact_timestamp(row) if row else None
        db.commit()

    if row is not None:
        move(user_id, source, change.destination, change.directory)
        restored = snapshot(user_id).get(change.destination)
        if restored:
            restored["version"] = version
            if change.directory:
                restored["signature"] = signature(destination)
            remember(user_id, change.destination, restored)
    return row is not None
