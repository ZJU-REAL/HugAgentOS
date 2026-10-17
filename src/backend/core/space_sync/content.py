"""Personal content persistence with immutable objects and invalidated derived content."""

import hashlib
import mimetypes
import uuid

from core.db.models import Artifact, UserShadow
from core.infra.time import utc_now
from core.services.folder_batch import lock_folder_owner

from .personal_metadata import domain


def signature(path):
    st = path.stat()
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def _artifact_at(db, scope, path, *, lock=False):
    """Resolve a path without retaining a row lock during object-store I/O."""
    folder, _, name = path.rpartition("/")
    directory = scope.folders(db).get(folder)
    if folder and directory is None:
        return None
    query = (
        db.query(Artifact)
        .filter(
            *scope.artifact_predicates,
            Artifact.user_folder_id == (directory.folder_id if directory else None),
            Artifact.filename == name,
            Artifact.deleted_at.is_(None),
        )
        .populate_existing()
    )
    # Other writers can acquire artifact -> owner. Never wait in reverse order.
    return (query.with_for_update(nowait=True) if lock else query).one_or_none()


def _revision(art):
    if art is None:
        return None
    return (
        art.artifact_id,
        art.storage_key,
        art.updated_at,
        art.created_at,
        art.user_folder_id,
        art.filename,
    )


def _remember_registration(user_id, rel, art, digest, observed):
    from .index import remember

    result = {
        "file_id": art.artifact_id,
        "storage_key": art.storage_key,
        "name": art.filename,
        "size": art.size_bytes,
    }
    remember(
        user_id,
        rel,
        {
            "id": art.artifact_id,
            "key": art.storage_key,
            "sha256": digest,
            "signature": observed,
            "directory": False,
            "version": artifact_timestamp(art),
        },
    )
    return result


def register(user_id, entry):
    from core.db.engine import SessionLocal
    from core.services.project_source import reserve_sqlite_writer
    from core.storage import get_storage
    from core.config.settings import settings
    from core.sandbox._common import myspace_cache_dir
    from fastapi import HTTPException
    from sqlalchemy.exc import OperationalError

    from .files import read_source
    from .index import get

    before = signature(entry.path)
    data = read_source(
        myspace_cache_dir(user_id), entry.rel, max_bytes=settings.sandbox.artifact_max_bytes
    )
    if before != signature(entry.path):
        raise RuntimeError("File changed during synchronization")
    digest = hashlib.sha256(data).hexdigest()
    baseline = get(user_id, entry.rel)
    scope = domain(user_id)
    # Copy only immutable values; close the read transaction before remote I/O.
    with SessionLocal() as db:
        art = _artifact_at(db, scope, entry.rel)
        expected = _revision(art)
        observed_version = artifact_timestamp(art) if art else None
        observed_digest = (
            (art.extra_data or {}).get("space_sync", {}).get("sha256") if art else None
        )
    storage = get_storage()
    if expected and baseline:
        if baseline.get("id") != expected[0] or baseline.get("key") != expected[1]:
            if observed_digest != digest:
                raise HTTPException(409, "空间文件已变更，本地修改已保留")
        elif baseline.get("version") is not None and baseline["version"] != observed_version:
            cloud_hash = hashlib.sha256(storage.download_bytes(expected[1])).hexdigest()
            if cloud_hash not in (baseline.get("sha256"), digest):
                raise HTTPException(409, "空间内容版本已变更，本地修改已保留")
    adopting = bool(
        expected
        and baseline
        and (baseline.get("id") != expected[0] or baseline.get("key") != expected[1])
    )
    artifact_id = expected[0] if expected else "ms_" + uuid.uuid4().hex
    key = None if adopting else f"myspace/{user_id}/{artifact_id}/{uuid.uuid4().hex}"
    committed = False
    try:
        # Upload an immutable object before taking any database writer lock.
        url = storage.upload_bytes(data, key) if key else None
        if before != signature(entry.path):
            raise RuntimeError("File changed during upload")
        with SessionLocal() as db:
            reserve_sqlite_writer(db)
            lock_folder_owner(db, UserShadow, "user_id", user_id)
            art = _artifact_at(db, scope, entry.rel, lock=True)
            if _revision(art) != expected:
                raise HTTPException(409, "空间文件在同步期间已变更，本地修改已保留")
            if before != signature(entry.path):
                raise RuntimeError("File changed before registration")
            if (
                art
                and baseline
                and (
                    baseline.get("id") != art.artifact_id or baseline.get("key") != art.storage_key
                )
            ):
                # The earlier cloud snapshot already contained these exact bytes.
                result = _remember_registration(user_id, entry.rel, art, digest, before)
            else:
                folder, _, name = entry.rel.rpartition("/")
                if art is None:
                    art = Artifact(
                        artifact_id=artifact_id,
                        user_id=user_id,
                        user_folder_id=scope.directory(db, folder),
                        filename=name,
                        title=name,
                        type="other",
                        size_bytes=max(1, len(data)),
                        storage_key="",
                    )
                    db.add(art)
                art.storage_key, art.storage_url = key, url
                art.size_bytes = max(1, len(data))
                art.mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
                art.updated_at = utc_now()
                art.parsed_text = art.summary = art.parsed_at = art.parse_error = None
                art.extra_data = {
                    **(art.extra_data or {}),
                    "space_sync": {
                        "sha256": digest,
                        "signature": before,
                        "key": key,
                    },
                }
                db.flush()
                # Build the index entry before commit expires ORM attributes.
                state = {
                    "id": art.artifact_id,
                    "key": key,
                    "sha256": digest,
                    "signature": before,
                    "directory": False,
                    "version": artifact_timestamp(art),
                }
                result = {
                    "file_id": art.artifact_id,
                    "storage_key": key,
                    "name": name,
                    "size": len(data),
                }
                db.commit()
                committed = True
                from .index import remember

                remember(user_id, entry.rel, state)
        return result
    except OperationalError as exc:
        original = exc.orig
        code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        if code == "55P03" or "database is locked" in str(original):
            raise HTTPException(409, "空间文件正被修改，请重试；本地修改已保留") from exc
        raise
    finally:
        if key and not committed:
            # Failed publication leaves an immutable object with no database reference.
            storage.delete(key)


def artifact_timestamp(art):
    from core.infra.time import as_utc

    stamp = art.updated_at or art.created_at
    return as_utc(stamp).timestamp() if stamp else None


def current_snapshot(art, entry):
    """Consult the materialized version, including files originally uploaded by the UI."""
    if art is None or not getattr(art, "user_id", None):
        return None
    from core.sandbox._common import myspace_cache_dir

    from .files import read_source
    from .index import get, remember

    baseline = get(art.user_id, entry.rel)
    registered = (art.extra_data or {}).get("space_sync")
    state = baseline or registered
    if not state:
        from core.storage import get_storage

        cloud = get_storage().download_bytes(art.storage_key)
        digest = hashlib.sha256(cloud).hexdigest()
        clean = (
            hashlib.sha256(read_source(myspace_cache_dir(art.user_id), entry.rel)).hexdigest()
            == digest
        )
        remember(
            art.user_id,
            entry.rel,
            {
                "id": art.artifact_id,
                "key": art.storage_key,
                "sha256": digest,
                "signature": signature(entry.path) if clean else None,
                "directory": False,
                "version": artifact_timestamp(art),
            },
        )
        return clean
    if baseline and baseline.get("id") != art.artifact_id:
        return False
    observed = signature(entry.path)
    clean = state.get("signature") == observed
    if not clean:
        clean = hashlib.sha256(
            read_source(myspace_cache_dir(art.user_id), entry.rel)
        ).hexdigest() == state.get("sha256")
    if not clean and (
        state.get("key") != art.storage_key or state.get("version") != artifact_timestamp(art)
    ):
        if adopt_cloud_bytes(art, entry):
            return True
    # A changed cloud revision may only be projected over the old clean snapshot.
    if state.get("key") != art.storage_key:
        return clean
    if (
        clean
        and registered
        and registered.get("key") == art.storage_key
        and (not baseline or baseline.get("key") != art.storage_key)
    ):
        remember(
            art.user_id,
            entry.rel,
            {
                "id": art.artifact_id,
                "key": art.storage_key,
                "sha256": registered["sha256"],
                "signature": observed,
                "directory": False,
                "version": artifact_timestamp(art),
            },
        )
    return clean


def adopt_cloud_bytes(art, entry):
    """An API may already have materialized new bytes; adopt only a stable exact match."""
    from core.sandbox._common import myspace_cache_dir
    from core.storage import get_storage

    from .files import read_source
    from .index import remember

    before = signature(entry.path)
    local = hashlib.sha256(read_source(myspace_cache_dir(art.user_id), entry.rel)).hexdigest()
    cloud = hashlib.sha256(get_storage().download_bytes(art.storage_key)).hexdigest()
    if local != cloud or signature(entry.path) != before:
        return False
    remember(
        art.user_id,
        entry.rel,
        {
            "id": art.artifact_id,
            "key": art.storage_key,
            "sha256": cloud,
            "signature": before,
            "directory": False,
            "version": artifact_timestamp(art),
        },
    )
    return True
