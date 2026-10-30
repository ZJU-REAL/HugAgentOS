"""Public branding and manual assets, persisted with the configured storage."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import uuid
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from core.files.storage import get_storage

PREFIX = "public-content"


def remote() -> bool:
    return os.getenv("STORAGE_TYPE", "local").lower() in ("s3", "oss", "obs")


def key(kind: str, filename: str) -> str:
    manual = kind == "manual" and filename == "manual.pdf"
    image = kind == "page_config" and re.fullmatch(
        r"(?:logo|favicon|app_icon|mcp_icon)_[0-9a-f]{12}\.(?:png|jpg|svg|webp|ico)", filename
    )
    if not (manual or image):
        raise HTTPException(404, "Asset not found")
    return f"{PREFIX}/{kind}/{filename}"


def url(kind: str, filename: str) -> str:
    key(kind, filename)
    return f"/api/v1/content/public-assets/{kind}/{filename}"


def upload(kind: str, filename: str, content: bytes) -> str:
    get_storage().upload_bytes(content, key(kind, filename))
    return url(kind, filename)


def manual_info():
    item = get_storage().stat_object(key("manual", "manual.pdf"))
    if item is None:
        return {"exists": False}
    return {"exists": True, "filename": "manual.pdf", "size": item.size,
            "uploaded_at": item.modified_at.isoformat() if item.modified_at else None,
            "url": url("manual", "manual.pdf")}


def _cache_root() -> Path:
    return Path(tempfile.gettempdir()) / "hugagent-public-assets"


def _cached_copy(storage, storage_key: str, item) -> Path:
    """Return a local copy of the current object version, downloading it at most once.

    The copy is a disposable cache in the container's temporary directory, keyed
    by the object's version metadata; object storage stays the only durable store.
    Range requests are then answered from the local file instead of re-downloading
    the whole object for every slice a PDF viewer asks for.
    """
    root = _cache_root()
    root.mkdir(mode=0o700, exist_ok=True)
    stem = hashlib.sha256(storage_key.encode()).hexdigest()[:32]
    version = item.etag or f"{item.size}:{item.modified_at.isoformat() if item.modified_at else ''}"
    tag = hashlib.sha256(f"{version}\0{item.size}".encode()).hexdigest()[:32]
    path = root / f"{stem}-{tag}{Path(storage_key).suffix}"
    if path.is_file() and path.stat().st_size == item.size:
        return path
    handle = tempfile.NamedTemporaryFile(dir=root, prefix=".download-", delete=False)
    temporary = Path(handle.name)
    handle.close()
    try:
        storage.download(storage_key, str(temporary))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    for stale in root.glob(f"{stem}-*"):
        if stale != path:
            stale.unlink(missing_ok=True)
    return path


def response(kind: str, filename: str):
    storage_key = key(kind, filename)
    storage = get_storage()
    item = storage.stat_object(storage_key)
    if item is None:
        raise HTTPException(404, "Asset not found")
    # Each response reads its own hard link, so replacing the cached version
    # while a slow download is still in flight never breaks that download.
    for _ in range(2):
        path = _cached_copy(storage, storage_key, item)
        serving = path.with_name(f".serving-{uuid.uuid4().hex}{path.suffix}")
        try:
            os.link(path, serving)
            break
        except FileNotFoundError:  # a newer version replaced it just now
            item = storage.stat_object(storage_key)
            if item is None:
                raise HTTPException(404, "Asset not found")
    else:
        raise HTTPException(503, "Asset is being replaced, retry")
    headers = {"X-Content-Type-Options": "nosniff",
               "Cache-Control": "no-cache" if kind == "manual" else "public, max-age=31536000, immutable"}
    if kind != "manual":
        # Uploaded SVG images must never run script. The manual keeps the headers it
        # had as a static file: browsers refuse to open a PDF under a CSP sandbox.
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return FileResponse(serving, filename=filename, content_disposition_type="inline", headers=headers,
                        background=BackgroundTask(serving.unlink, missing_ok=True))
