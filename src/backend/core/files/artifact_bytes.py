"""Shared model/file contracts without execution-layer dependencies."""
from __future__ import annotations
import logging
from typing import Any, Dict, Optional
logger = logging.getLogger(__name__)
def is_image(f: Dict[str, Any]) -> bool:
    mime = (f.get("mime_type") or "").lower()
    return mime.startswith("image/")

def download_artifact_bytes(
    file_id: str,
    fallback_name: str,
    log_prefix: str,
    user_id: Optional[str] = None,
) -> Optional[bytes]:
    """Resolve an artifact's storage key and download its raw bytes.

    Returns None on any failure (missing artifact, storage error, etc.) and
    logs at WARNING. Used by the image and xlsx-preview hook branches that
    both need raw bytes (not parsed text).

    When ``user_id`` is supplied, the artifact's owner is verified via
    :func:`load_artifact_meta` before any storage round-trip. This is the
    defense-in-depth gate for hook-level reads: the agent context is built
    from ``request.attachments[].file_id`` (client-supplied), and the
    backend boundary doesn't re-verify ownership before reaching the
    hook. Without this check, a forged ``file_id`` belonging to another
    user would have its raw bytes downloaded and silently materialized
    into the agent's memory (image base64 or xlsx preview block). The
    matching ``fetch_parsed_text`` already does the same check for parsed
    text — this brings the binary paths to parity.

    ``user_id=None`` preserves the legacy "trust the caller" behavior
    used by ``core.llm.tools.read_artifact_tool._probe_xlsx_sheet_names`` /
    ``_probe_pptx_slide_count`` where ``fetch_parsed_text(user_id=...)``
    is the authoritative gate downstream.
    """
    from core.files.artifacts.reader import load_artifact_meta, resolve_artifact_storage
    from core.infra.exceptions import StorageError
    from core.files.storage import get_storage

    if user_id:
        meta = load_artifact_meta(file_id, user_id=user_id)
        if meta is None:
            logger.warning(
                "%s: artifact %s denied for user %s (not owner / missing / deleted)",
                log_prefix,
                file_id,
                user_id,
            )
            return None

    from core.files.artifacts.local_reference import authorized_reference, is_local_file_ref
    if is_local_file_ref(file_id):
        item = authorized_reference(file_id, user_id)
        if not item:
            return None
        try:
            from pathlib import Path
            return Path(item["path"]).read_bytes()
        except OSError:
            return None

    storage_key, _ = resolve_artifact_storage(file_id, fallback_name)
    if not storage_key:
        return None

    try:
        return get_storage().download_bytes(storage_key)
    except StorageError as e:
        logger.warning(f"{log_prefix}: storage download failed for {file_id}: {e}")
        return None
    except Exception as e:
        logger.warning(f"{log_prefix}: unexpected download error for {file_id}: {e}")
        return None
