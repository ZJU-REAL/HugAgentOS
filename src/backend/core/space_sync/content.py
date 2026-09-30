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


def register(user_id, entry):
    from core.db.engine import SessionLocal
    from core.services.project_source import reserve_sqlite_writer
    from core.storage import get_storage

    before = signature(entry.path)
    from core.config.settings import settings
    from core.sandbox._common import myspace_cache_dir

    from .files import read_source

    data = read_source(
        myspace_cache_dir(user_id), entry.rel, max_bytes=settings.sandbox.artifact_max_bytes
    )
    if before != signature(entry.path):
        raise RuntimeError("File changed during synchronization")
    storage = get_storage()
    key = None
    committed = False
    try:
        with SessionLocal() as db:
            reserve_sqlite_writer(db)
            lock_folder_owner(db, UserShadow, "user_id", user_id)
            scope = domain(user_id)
            art = scope.artifact(db, entry.rel)
            folder, _, name = entry.rel.rpartition("/")
            from .index import get

            baseline = get(user_id, entry.rel)
            content_hash = hashlib.sha256(data).hexdigest()
            if (
                art
                and baseline
                and baseline.get("key") == art.storage_key
                and baseline.get("version") is not None
            ):
                from core.myspace.mirror import _artifact_ts

                if baseline["version"] != _artifact_ts(art):
                    cloud_hash = hashlib.sha256(storage.download_bytes(art.storage_key)).hexdigest()
                    if cloud_hash not in (baseline.get("sha256"), content_hash):
                        from fastapi import HTTPException

                        raise HTTPException(409, "空间内容版本已变更，本地修改已保留")
            if (
                art
                and baseline
                and (
                    baseline.get("id") != art.artifact_id or baseline.get("key") != art.storage_key
                )
            ):
                if (art.extra_data or {}).get("space_sync", {}).get("sha256") != content_hash:
                    from fastapi import HTTPException

                    raise HTTPException(409, "空间文件已变更，本地修改已保留")
                from .index import remember

                remember(
                    user_id,
                    entry.rel,
                    {
                        "id": art.artifact_id,
                        "key": art.storage_key,
                        "sha256": content_hash,
                        "signature": before,
                        "directory": False,
                        "version": artifact_timestamp(art),
                    },
                )
                return {
                    "file_id": art.artifact_id,
                    "storage_key": art.storage_key,
                    "name": name,
                    "size": len(data),
                }
            if art is None:
                art = Artifact(
                    artifact_id="ms_" + uuid.uuid4().hex,
                    user_id=user_id,
                    user_folder_id=scope.directory(db, folder),
                    filename=name,
                    title=name,
                    type="other",
                    size_bytes=max(1, len(data)),
                    storage_key="",
                )
                db.add(art)
            key = f"myspace/{user_id}/{art.artifact_id}/{uuid.uuid4().hex}"
            url = storage.upload_bytes(data, key)
            if before != signature(entry.path):
                raise RuntimeError("File changed during upload")
            art.storage_key, art.storage_url = key, url
            art.size_bytes = max(1, len(data))
            art.mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            art.updated_at = utc_now()
            art.parsed_text = art.summary = art.parsed_at = art.parse_error = None
            art.extra_data = {
                **(art.extra_data or {}),
                "space_sync": {
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "signature": before,
                    "key": key,
                },
            }
            db.commit()
            committed = True
            from .index import remember

            remember(
                user_id,
                entry.rel,
                {
                    "id": art.artifact_id,
                    "key": key,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "signature": before,
                    "directory": False,
                    "version": artifact_timestamp(art),
                },
            )
            return {"file_id": art.artifact_id, "storage_key": key, "name": name, "size": len(data)}
    except Exception:
        if key and not committed:
            storage.delete(key)
        raise


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
