"""Shared helpers for reading artifact contents and classifying them by source.

All functions are defensive: they return empty/None on failure so callers
(hooks, tools) can degrade gracefully without wrapping every call in try/except.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# Canonical source values stored in Artifact.extra_data["source"].
SOURCE_USER_UPLOAD = "user_upload"
SOURCE_AI_GENERATED = "ai_generated"

# Variants treated as ai_generated (set by different tool pipelines).
_AI_SOURCES = {SOURCE_AI_GENERATED, "code_exec_direct", "skill_script", "code_exec"}


def infer_source(art) -> str:
    """Classify an Artifact row as user_upload vs ai_generated.

    Checks extra_data["source"] first; falls back to the artifact_id prefix
    convention (rows created before the source field was introduced).
    """
    extra = art.extra_data or {}
    src = (extra.get("source") or "").strip()
    if src == SOURCE_USER_UPLOAD:
        return SOURCE_USER_UPLOAD
    if src in _AI_SOURCES:
        return SOURCE_AI_GENERATED
    if (art.artifact_id or "").startswith("ua_"):
        return SOURCE_USER_UPLOAD
    return SOURCE_AI_GENERATED


def resolve_artifact_storage(
    file_id: str,
    fallback_filename: str = "file",
) -> Tuple[Optional[str], str]:
    """Resolve (storage_key, filename) by file_id.

    Checks the in-process artifact registry first (has extension in key),
    then the DB. Returns (None, fallback_filename) if neither finds a match.
    """
    storage_key: Optional[str] = None
    filename = fallback_filename

    try:
        from core.artifacts.store import get_artifact
        item = get_artifact(file_id)
        if item and item.get("storage_key"):
            storage_key = item["storage_key"]
            filename = item.get("name") or filename
    except Exception:
        pass

    if not storage_key:
        try:
            from core.db.engine import SessionLocal
            from core.db.models import Artifact as ArtifactModel
            with SessionLocal() as db:
                artifact_obj = db.query(ArtifactModel).filter(
                    ArtifactModel.artifact_id == file_id
                ).first()
                if artifact_obj and artifact_obj.storage_key:
                    storage_key = artifact_obj.storage_key
                    filename = artifact_obj.filename or filename
        except Exception as e:
            logger.warning(f"Artifact lookup: DB query failed for {file_id}: {e}")

    return storage_key, filename


def fetch_parsed_text(
    file_id: str,
    user_id: Optional[str] = None,
    *,
    prefetched_bytes: Optional[bytes] = None,
) -> str:
    """Return the artifact's parsed text, populating Artifact.parsed_text on miss.

    Returns "" on any failure (missing, deleted, unauthorized, parse error).
    Parse errors are persisted to Artifact.parse_error to avoid retry churn.

    When ``prefetched_bytes`` is supplied, it's used as the input on cache
    miss so callers that already downloaded the file (e.g. read_artifact's
    sheet/slide probes) don't pay for a second storage round-trip.
    """
    if not file_id:
        return ""

    try:
        from core.db.engine import SessionLocal
        from core.db.models import Artifact as ArtifactModel
    except Exception as e:
        logger.warning(f"artifact_reader: DB imports failed: {e}")
        return ""

    with SessionLocal() as db:
        art = db.query(ArtifactModel).filter(
            ArtifactModel.artifact_id == file_id
        ).first()
        if art is None or art.deleted_at is not None:
            return ""
        if user_id and art.user_id != user_id:
            logger.warning(
                f"artifact_reader: user {user_id} denied access to {file_id} (owner={art.user_id})"
            )
            return ""

        if art.parsed_text:
            return art.parsed_text

        storage_key = art.storage_key
        filename = art.filename or "file"

        try:
            from core.content.file_parser import parse_file
            from core.infra.exceptions import StorageError
            from core.storage import get_storage

            file_bytes = (
                prefetched_bytes
                if prefetched_bytes is not None
                else get_storage().download_bytes(storage_key)
            )
            parsed = parse_file(file_bytes, filename) or ""
        except (StorageError, RuntimeError) as e:
            err_msg = f"{type(e).__name__}: {e}"
            logger.warning(
                f"artifact_reader: parse failed for {filename} ({file_id}): {err_msg}"
            )
            art.parse_error = err_msg[:500]
            db.commit()
            return ""
        except Exception as e:
            logger.warning(
                f"artifact_reader: unexpected parse error for {filename} ({file_id}): {e}"
            )
            return ""

        if parsed:
            art.parsed_text = parsed
            art.parsed_at = datetime.utcnow()
            art.parse_error = None
            db.commit()
        return parsed


def load_artifact_meta(file_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return lightweight artifact metadata (no parsed_text), or None if inaccessible."""
    if not file_id:
        return None
    try:
        from core.db.engine import SessionLocal
        from core.db.models import Artifact as ArtifactModel
    except Exception as e:
        logger.warning(f"artifact_reader: DB imports failed: {e}")
        return None

    with SessionLocal() as db:
        art = db.query(ArtifactModel).filter(
            ArtifactModel.artifact_id == file_id
        ).first()
        if art is None or art.deleted_at is not None:
            return None
        if user_id and art.user_id != user_id:
            return None

        return {
            "file_id": art.artifact_id,
            "filename": art.filename,
            "name": art.filename,
            "mime_type": art.mime_type,
            "size_bytes": art.size_bytes,
            "summary": art.summary or "",
            "source": infer_source(art),
            "parse_error": art.parse_error,
            "has_parsed_text": bool(art.parsed_text),
        }
