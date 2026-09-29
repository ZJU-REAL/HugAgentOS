"""Artifact store with local / OSS dual-mode support.

Storage mode is controlled by the STORAGE_TYPE environment variable:
- 'local' (default): files are written under
  ``${STORAGE_PATH:-result}/artifacts/`` on the local filesystem and served
  directly via FileResponse.
- 'oss': files are uploaded to Aliyun OSS via OSSStorageBackend.

Metadata is one JSON file per artifact (see :func:`_record_path`); in OSS mode
each record is mirrored alongside the bytes so the registry survives losing the
local volume.

Artifacts are downloaded via ``GET /files/{file_id}``.
"""

from __future__ import annotations
from core.infra.time import utc_now

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

# ── Local paths ──────────────────────────────────────────────────────────────
# Prefer STORAGE_PATH (usually /app/storage inside the container) to avoid creating
# a no-permission directory under /app; fall back to the project root result/ when unset.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_STORAGE_BASE = os.getenv("STORAGE_PATH", "").strip()
_BASE_DIR = Path(_STORAGE_BASE).expanduser() if _STORAGE_BASE else (_PROJECT_ROOT / "result")
_STORE_DIR = (_BASE_DIR / "artifacts").resolve()

# ── Record layout ────────────────────────────────────────────────────────────
# One JSON file per artifact, bucketed by the first two hex characters of its
# id: ``artifacts/records/<xx>/<file_id>.json``.
#
# This used to be a single ``index.json`` holding every record. Registering one
# new artifact rewrote that whole file and re-uploaded it to OSS, and every
# single lookup re-read and re-parsed it — 12.9 MB and 26k records on the
# production install, both costs growing with the install and both paid on the
# request path. Records are independent rows and nothing ever enumerates them
# during normal operation, so giving each its own file makes reads and writes
# O(1) no matter how large the install gets.
# Every path below derives from _STORE_DIR so that redirecting the store (tests,
# a relocated STORAGE_PATH) is one substitution rather than a set of globals that
# have to be kept in step.
_RECORDS_DIRNAME = "records"
_LEGACY_INDEX_NAME = "index.json"
_SHARD_LEN = 2

# Record keys in object storage (the backend adds its own prefix).
_OSS_RECORD_PREFIX = "artifacts/_records"
# Key of the monolithic index an install had before records were split out. It is
# still the only object-storage copy of everything written before the split, so the
# restore path reads it.
_OSS_LEGACY_INDEX_KEY = "artifacts/_index.json"

# Bucket directories already created in this process (see _ensure_bucket).
_known_buckets: set = set()


# ── Helper: get the current STORAGE_TYPE ──────────────────────────────────
def _storage_type() -> str:
    return os.getenv("STORAGE_TYPE", "local").lower()


# ── Helper: get the OSS backend (lazy import to avoid circular deps) ────────────────
def _get_oss_storage():
    from core.storage import get_storage

    return get_storage()


# ── Index management ─────────────────────────────────────────────────────


def _now_iso() -> str:
    return utc_now().isoformat()


def _ensure_store() -> None:
    """Make sure the local artifact directory exists."""
    _STORE_DIR.mkdir(parents=True, exist_ok=True)


def _shard(file_id: str) -> str:
    """Bucket name for an id. One definition so local and OSS layouts agree."""
    return file_id[:_SHARD_LEN]


def _legacy_index_path() -> Path:
    return _STORE_DIR / _LEGACY_INDEX_NAME


def _record_path(file_id: str) -> Path:
    return _STORE_DIR / _RECORDS_DIRNAME / _shard(file_id) / f"{file_id}.json"


def _record_oss_key(file_id: str) -> str:
    return f"{_OSS_RECORD_PREFIX}/{_shard(file_id)}/{file_id}.json"


def _read_record(file_id: str) -> Optional[Dict[str, Any]]:
    try:
        raw = _record_path(file_id).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        item = json.loads(raw)
    except ValueError:
        logger.warning("Artifact record is not valid JSON: %s", file_id)
        return None
    return item if isinstance(item, dict) else None


def _ensure_bucket(path: Path) -> None:
    """Create a record's bucket directory once per process.

    There are only ``16 ** _SHARD_LEN`` buckets, so a mkdir per write is the same
    syscall repeated thousands of times during a migration. Keyed on the real path
    so redirecting _STORE_DIR (tests, a moved STORAGE_PATH) still creates it.
    """
    if path in _known_buckets:
        return
    path.mkdir(parents=True, exist_ok=True)
    _known_buckets.add(path)


def _write_record_local(item: Dict[str, Any]) -> str:
    """Write one record to disk atomically. Returns the serialised text."""
    path = _record_path(str(item["file_id"]))
    _ensure_bucket(path.parent)
    text = json.dumps(item, ensure_ascii=False)
    # Write to a sibling then rename: a concurrent reader sees either the old
    # record or the new one, never a half-written file.
    tmp = path.with_name(f".{path.name}.{uuid4().hex}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return text


def _write_record(item: Dict[str, Any]) -> None:
    file_id = str(item["file_id"])
    text = _write_record_local(item)
    if _storage_type() == "oss":
        try:
            _get_oss_storage().upload_bytes(text.encode("utf-8"), _record_oss_key(file_id))
        except Exception as exc:  # noqa: BLE001 — the local record is authoritative
            logger.warning("Failed to mirror artifact record %s to OSS: %s", file_id, exc)


def migrate_legacy_index() -> int:
    """Split a legacy monolithic ``index.json`` into per-artifact records.

    Idempotent and restartable: records already on disk are left alone, and the
    legacy file is only retired once every one of its entries has been written
    out, so an interrupted run simply resumes. Returns the number of records
    written. Call this off the request path — see the startup step in api/app.py.
    """
    legacy = _legacy_index_path()
    if not legacy.exists():
        return 0
    try:
        raw = legacy.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError) as exc:
        logger.error("Legacy artifact index unreadable, not migrating: %s", exc)
        return 0
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        logger.error("Legacy artifact index has no 'files' map, not migrating")
        return 0

    written = 0
    for file_id, item in files.items():
        if not isinstance(item, dict) or not file_id:
            continue
        if _record_path(str(file_id)).exists():
            continue
        item.setdefault("file_id", file_id)
        # Local only. Mirroring here would be one OSS PUT per record — 26k serial
        # round-trips inside the startup gate — and the legacy index is already in
        # object storage; records re-mirror on their next write.
        _write_record_local(item)
        written += 1

    retired = legacy.with_suffix(legacy.suffix + ".migrated")
    legacy.replace(retired)
    logger.info(
        "Artifact index migrated to per-record files: %d written, %d total, legacy kept at %s",
        written,
        len(files),
        retired,
    )
    return written


def _has_records() -> bool:
    return any((_STORE_DIR / _RECORDS_DIRNAME).glob("*/*.json"))


def restore_records_from_oss() -> int:
    """Rebuild the local record files from the OSS mirror.

    For the case where the storage volume is lost but object storage survives —
    the local-index-restored-from-OSS behaviour the monolithic index used to have.
    Enumerates every mirrored record, so it only ever runs from :func:`prepare_records`.
    """
    if _storage_type() != "oss":
        return 0
    storage = _get_oss_storage()
    restored = 0
    for key in storage.list_keys(_OSS_RECORD_PREFIX):
        file_id = Path(key).stem
        if not file_id or _record_path(file_id).exists():
            continue
        try:
            item = json.loads(storage.download_bytes(key).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — one bad object must not stop the restore
            logger.warning("Skipping unreadable OSS artifact record %s: %s", key, exc)
            continue
        if isinstance(item, dict):
            _write_record_local(item)
            restored += 1

    # Anything written before the split only exists in object storage as the old
    # monolithic index; without this those artifacts would not come back.
    try:
        blob = storage.download_bytes(_OSS_LEGACY_INDEX_KEY)
    except Exception:  # noqa: BLE001 — absent on installs that never had one
        blob = None
    if blob:
        legacy = _legacy_index_path()
        legacy.write_bytes(blob)
        restored += migrate_legacy_index()

    logger.info("Restored %d artifact records from OSS", restored)
    return restored


def _record_artifact(item: Dict[str, Any]) -> None:
    """Register one artifact. Costs one small file write, whatever the install size."""
    _write_record(item)


# ── Public API ──────────────────────────────────────────────────────


def save_artifact_bytes(
    *,
    content: bytes,
    name: str,
    mime_type: str = "application/octet-stream",
    extension: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist byte content and return a metadata dict containing file_id / storage_key.

    local mode: write to local ${STORAGE_PATH:-result}/artifacts/
    oss   mode: upload to OSS, keep only the index entry locally
    """
    ext = extension.strip().lstrip(".")

    # Auto-fit SVG diagrams: models routinely under-size the root viewBox (the
    # bottom layer/legend gets clipped) and omit width/height. Expand-only and
    # fail-safe — returns the input untouched for non-SVG or on any parse error.
    if "svg" in mime_type.lower() or ext.lower() == "svg":
        try:
            from core.content.svg_fit import fit_svg_viewbox

            content = fit_svg_viewbox(content)
        except Exception as exc:  # never block a save on the normaliser
            logger.warning(f"svg viewBox auto-fit skipped: {exc}")

    file_id = uuid4().hex
    filename = f"{file_id}.{ext}" if ext else file_id

    mode = _storage_type()

    if mode == "oss":
        # ── OSS storage ──────────────────────────────────────────────
        storage_key = f"artifacts/{filename}"
        try:
            storage = _get_oss_storage()
            storage.upload_bytes(content, storage_key)
            logger.info(f"Artifact uploaded to OSS: {storage_key}")
        except Exception as e:
            logger.error(f"Failed to upload artifact to OSS: {e}")
            raise

        item: Dict[str, Any] = {
            "file_id": file_id,
            "name": name,
            "mime_type": mime_type,
            "size": len(content),
            "path": None,  # no local path in OSS mode
            "storage_key": storage_key,
            "created_at": _now_iso(),
            "metadata": metadata or {},
        }
    else:
        # ── Local storage (original logic) ───────────────────────────────────
        abs_path = (_STORE_DIR / filename).resolve()
        _ensure_store()
        abs_path.write_bytes(content)
        logger.info(f"Artifact saved locally: {abs_path}")

        item = {
            "file_id": file_id,
            "name": name,
            "mime_type": mime_type,
            "size": len(content),
            "path": str(abs_path),
            # Local mode also records a storage_key with extension (same shape as OSS mode: artifacts/<filename>).
            # Otherwise, on insert (artifact_service / _common's `ref.storage_key or f"artifacts/{id}"`)
            # it falls back to an extension-less key, and the download endpoint can't find the file in local storage by that key → HTTP 500.
            "storage_key": f"artifacts/{filename}",
            "created_at": _now_iso(),
            "metadata": metadata or {},
        }

    _record_artifact(item)

    return item


def save_artifact_file(
    *,
    file_path: str | Path,
    name: str,
    mime_type: str = "application/octet-stream",
    extension: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist a local file without loading its binary content into memory.

    Local storage copies the file into the artifact directory. OSS storage
    uses the backend's file-upload method, which streams from disk. SVG files
    retain the existing viewBox normalisation behavior and use the byte path.
    """
    source = Path(file_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Artifact source file not found: {source}")

    ext = extension.strip().lstrip(".")
    if "svg" in mime_type.lower() or ext.lower() == "svg":
        return save_artifact_bytes(
            content=source.read_bytes(),
            name=name,
            mime_type=mime_type,
            extension=ext,
            metadata=metadata,
        )

    size = source.stat().st_size
    file_id = uuid4().hex
    filename = f"{file_id}.{ext}" if ext else file_id
    storage_key = f"artifacts/{filename}"

    if _storage_type() == "oss":
        try:
            storage = _get_oss_storage()
            storage.upload(str(source), storage_key)
            logger.info("Artifact file uploaded to OSS: %s", storage_key)
        except Exception as exc:
            logger.error("Failed to upload artifact file to OSS: %s", exc)
            raise
        item: Dict[str, Any] = {
            "file_id": file_id,
            "name": name,
            "mime_type": mime_type,
            "size": size,
            "path": None,
            "storage_key": storage_key,
            "created_at": _now_iso(),
            "metadata": metadata or {},
        }
    else:
        abs_path = (_STORE_DIR / filename).resolve()
        _ensure_store()
        shutil.copyfile(source, abs_path)
        logger.info("Artifact file saved locally: %s", abs_path)
        item = {
            "file_id": file_id,
            "name": name,
            "mime_type": mime_type,
            "size": size,
            "path": str(abs_path),
            "storage_key": storage_key,
            "created_at": _now_iso(),
            "metadata": metadata or {},
        }

    _record_artifact(item)

    return item


def get_artifact(file_id: str) -> Optional[Dict[str, Any]]:
    """Look up artifact metadata by file_id."""
    if not file_id:
        return None
    item = _read_record(file_id)
    if item is None:
        return None

    if (item.get("metadata") or {}).get("source") == "local_project_reference":
        from .local_project import resolve_project_reference

        return resolve_project_reference(item)

    # Local mode (item carries a local path): confirm the file actually exists to avoid a dangling index.
    # Note: local entries now also carry storage_key, so we must first check existence by path,
    # and not pass just because storage_key is non-empty (otherwise a deleted file would still be judged valid).
    local_path = item.get("path")
    if local_path:
        path = Path(str(local_path))
        if not path.exists() or not path.is_file():
            return None
        return item

    # OSS mode (no local path): as long as storage_key exists, treat it as valid
    if item.get("storage_key"):
        return item
    return None


def prepare_records() -> int:
    """Bring the local record set up to date. Returns how many records it wrote.

    Covers the two one-time situations that leave it behind: an install upgrading
    from the monolithic ``index.json``, and an install whose storage volume was
    lost while its OSS mirror survived. Both enumerate everything, so this runs
    from the startup step in api/app.py and never from a request.
    """
    _ensure_store()
    written = migrate_legacy_index()
    if written or _has_records():
        return written
    return restore_records_from_oss()
