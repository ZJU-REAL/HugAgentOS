"""Artifact store with local / OSS dual-mode support.

Storage mode is controlled by the STORAGE_TYPE environment variable:
- 'local' (default): files are written under
  ``${STORAGE_PATH:-result}/artifacts/`` on the local filesystem and served
  directly via FileResponse.
- 'oss': files are uploaded to Aliyun OSS via OSSStorageBackend.  A local
  JSON index is still maintained for fast look-ups, and it is also backed up
  to OSS so that the index survives container restarts.

Artifacts are downloaded via ``GET /files/{file_id}``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

# ── Local path (both modes use it to store the index file) ────────────────────────
# Prefer STORAGE_PATH (usually /app/storage inside the container) to avoid creating
# a no-permission directory under /app; fall back to the project root result/ when unset.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_STORAGE_BASE = os.getenv("STORAGE_PATH", "").strip()
_BASE_DIR = Path(_STORAGE_BASE).expanduser() if _STORAGE_BASE else (_PROJECT_ROOT / "result")
_STORE_DIR = (_BASE_DIR / "artifacts").resolve()
_INDEX_PATH = _STORE_DIR / "index.json"
_LOCK = threading.Lock()

# Key of the index file in OSS (OSSStorageBackend adds the prefix automatically)
_OSS_INDEX_KEY = "artifacts/_index.json"


# ── Helper: get the current STORAGE_TYPE ──────────────────────────────────
def _storage_type() -> str:
    return os.getenv("STORAGE_TYPE", "local").lower()


# ── Helper: get the OSS backend (lazy import to avoid circular deps) ────────────────
def _get_oss_storage():
    from core.storage import get_storage
    return get_storage()


# ── Index management ─────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now().isoformat()


def _ensure_store() -> None:
    """Ensure the local dir and index file exist; in OSS mode, try to restore the index from OSS."""
    _STORE_DIR.mkdir(parents=True, exist_ok=True)

    if not _INDEX_PATH.exists():
        # OSS mode: try to restore the index from OSS (recover after a container restart)
        if _storage_type() == "oss":
            try:
                storage = _get_oss_storage()
                content = storage.download_bytes(_OSS_INDEX_KEY)
                _INDEX_PATH.write_bytes(content)
                logger.info("Artifact index restored from OSS.")
                return
            except Exception:
                pass  # first startup — nothing on OSS either, just create an empty index

        _INDEX_PATH.write_text(
            json.dumps({"files": {}}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _load_index() -> Dict[str, Any]:
    _ensure_store()
    try:
        raw = _INDEX_PATH.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        if isinstance(data, dict) and isinstance(data.get("files"), dict):
            return data
    except Exception:
        pass
    return {"files": {}}


def _save_index(data: Dict[str, Any]) -> None:
    _ensure_store()
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    _INDEX_PATH.write_text(text, encoding="utf-8")

    # OSS mode: back up the index in sync so it can be restored after a container restart
    if _storage_type() == "oss":
        try:
            storage = _get_oss_storage()
            storage.upload_bytes(text.encode("utf-8"), _OSS_INDEX_KEY)
        except Exception as e:
            logger.warning(f"Failed to backup artifact index to OSS: {e}")


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
            "path": None,           # no local path in OSS mode
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

    with _LOCK:
        index = _load_index()
        files = index.get("files")
        if not isinstance(files, dict):
            files = {}
            index["files"] = files
        files[file_id] = item
        _save_index(index)

    return item


def get_artifact(file_id: str) -> Optional[Dict[str, Any]]:
    """Look up artifact metadata by file_id."""
    with _LOCK:
        data = _load_index()
        files = data.get("files")
        if not isinstance(files, dict):
            return None
        item = files.get(file_id)
        if not isinstance(item, dict):
            return None

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
