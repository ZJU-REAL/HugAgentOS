"""Artifact reference helpers — pure data utilities over tool-result payloads.

Relocated out of ``api/routes/v1/artifacts.py`` so that lower layers
(``core.llm.tools``, ``core.services.artifact_service``, ``routing.*``) can use
them without importing an API route module (which previously forced a
``core/routing → api`` upward dependency). The API route now re-exports these.
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def infer_artifact_type(mime_type: str) -> str:
    """Map MIME type to Artifact.type enum value."""
    if mime_type.startswith("image/"):
        return "chart"
    if "wordprocessingml" in mime_type:
        return "report"
    return "document"


def resolve_artifact_storage_key(file_id: str, storage_key: Optional[str] = None) -> Optional[str]:
    """Resolve the real storage_key for an artifact.

    Tool results and historical DB rows may only store a placeholder key such as
    ``artifacts/<file_id>``. The artifact registry keeps the authoritative key,
    including the file extension required by OSS/local object lookup.
    """
    if not file_id:
        return storage_key

    try:
        from core.artifacts.store import get_artifact

        item = get_artifact(file_id)
        if item and item.get("storage_key"):
            return str(item["storage_key"])
    except Exception:
        logger.debug("resolve_artifact_storage_key: store lookup failed for %s", file_id, exc_info=True)

    return storage_key


def _normalize_file_ref(result: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(result, dict) or not result.get("file_id"):
        return None

    file_id = str(result["file_id"]).strip()
    url = str(result.get("url", result.get("download_url", ""))).strip()
    if not file_id or not url:
        return None

    return {
        "file_id": file_id,
        "name": str(result.get("name", "")).strip() or file_id,
        "mime_type": str(result.get("mime_type", "application/octet-stream")),
        "size": int(result.get("size", 0) or 0),
        "url": url,
        "storage_key": resolve_artifact_storage_key(file_id, result.get("storage_key")),
    }


def extract_file_refs(result: Any) -> List[Dict[str, Any]]:
    """Extract one or more normalized file refs from a tool result payload."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return []

    refs: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def _append(candidate: Any) -> None:
        ref = _normalize_file_ref(candidate)
        if not ref:
            return
        fid = ref["file_id"]
        if fid in seen:
            return
        seen.add(fid)
        refs.append(ref)

    if isinstance(result, list):
        for item in result:
            for ref in extract_file_refs(item):
                _append(ref)
        return refs

    if not isinstance(result, dict):
        return refs

    _append(result)

    for key in ("artifacts", "files"):
        values = result.get(key)
        if isinstance(values, list):
            for item in values:
                _append(item)

    nested = result.get("result")
    if nested is not None and nested is not result:
        for ref in extract_file_refs(nested):
            _append(ref)

    return refs


def extract_file_ref(result: Any) -> Optional[Dict[str, Any]]:
    """Backward-compatible single-file helper."""
    refs = extract_file_refs(result)
    return refs[0] if refs else None
