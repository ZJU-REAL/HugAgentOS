"""Shared low-level helpers for the LLM tool registrations.

Extracted from core/llm/tool.py so per-tool modules and the remaining register_*
functions share them. ``core.llm.tool`` re-exports these for compatibility.
"""

import base64
import json
import logging
from pathlib import Path
from typing import Any, Optional

from agentscope.message import TextBlock
from agentscope.tool._response import ToolChunk as ToolResponse

logger = logging.getLogger(__name__)


def _store_generated_files(
    files_data: list[dict[str, Any]],
    *,
    user_id: Optional[str],
    source: str,
    extra_metadata: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Persist sidecar-generated files and return normalized artifact refs."""
    if not files_data:
        return []

    try:
        from core.artifacts.store import save_artifact_bytes
    except Exception as exc:
        logger.warning("artifact store unavailable for %s: %s", source, exc)
        return []

    refs: list[dict[str, Any]] = []
    for fd in files_data:
        content_b64 = fd.get("content_b64", "")
        if not content_b64:
            continue

        name = str(fd.get("name", "output")).strip() or "output"
        mime_type = str(fd.get("mime_type", "application/octet-stream")).strip() or "application/octet-stream"
        try:
            content = base64.b64decode(content_b64)
        except Exception:
            logger.warning("skip invalid base64 artifact payload: %s", name)
            continue

        metadata: dict[str, Any] = {"source": source}
        if user_id:
            metadata["user_id"] = user_id
        if extra_metadata:
            metadata.update(extra_metadata)

        try:
            item = save_artifact_bytes(
                content=content,
                name=name,
                mime_type=mime_type,
                extension=Path(name).suffix.lstrip("."),
                metadata=metadata,
            )
        except Exception as exc:
            logger.warning("failed to persist generated artifact %s: %s", name, exc)
            continue

        refs.append({
            "file_id": item["file_id"],
            "name": item.get("name", name),
            "url": f"/files/{item['file_id']}",
            "mime_type": item.get("mime_type", mime_type),
            "size": item.get("size", len(content)),
            "storage_key": item.get("storage_key"),
        })

    return refs


def _summarize_generated_files(files_data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip binary payloads before exposing file metadata to the model/frontend."""
    summaries: list[dict[str, Any]] = []
    for fd in files_data or []:
        name = str(fd.get("name", "")).strip()
        if not name:
            continue
        summaries.append({
            "name": name,
            "mime_type": str(fd.get("mime_type", "application/octet-stream")),
            "size": int(fd.get("size", 0) or 0),
        })
    return summaries


MAX_ARTIFACT_FILE_SIZE = 10 * 1024 * 1024  # 10MB per artifact file


def _resolve_artifact_files(
    artifact_refs: dict[str, str],
    user_id: str | None,
) -> tuple[dict[str, str] | None, str | None]:
    """Resolve artifact:<id> references to base64-encoded binary content.

    Tries DB lookup first, falls back to local artifact store index.
    Returns (files_b64_dict, error_message).
    """
    if not artifact_refs:
        return None, None

    try:
        from core.storage.factory import get_storage
        from core.content.artifact_refs import resolve_artifact_storage_key
    except Exception as exc:
        return None, f"artifact 解析依赖不可用: {exc}"

    result: dict[str, str] = {}
    storage = get_storage()

    for filename, artifact_id in artifact_refs.items():
        storage_key = None
        owner_ok = True

        # Try 1: DB lookup
        try:
            from core.db.engine import SessionLocal
            from core.db.repository import ArtifactRepository

            db = SessionLocal()
            try:
                repo = ArtifactRepository(db)
                art = repo.get_by_id(artifact_id)
                if art:
                    if user_id and art.user_id != user_id:
                        owner_ok = False
                    else:
                        storage_key = resolve_artifact_storage_key(
                            art.artifact_id, art.storage_key
                        )
            finally:
                db.close()
        except Exception:
            pass

        if not owner_ok:
            return None, f"无权访问 artifact '{artifact_id}'"

        # Try 2: local artifact store fallback
        if not storage_key:
            try:
                from core.artifacts.store import get_artifact
                item = get_artifact(artifact_id)
                if item:
                    item_user = (item.get("metadata") or {}).get("user_id")
                    if user_id and item_user and item_user != user_id:
                        return None, f"无权访问 artifact '{artifact_id}'"
                    storage_key = item.get("storage_key")
            except Exception:
                pass

        if not storage_key:
            return None, f"artifact '{artifact_id}' 不存在或已删除"

        try:
            file_bytes = storage.download_bytes(storage_key)
        except Exception as exc:
            return None, f"artifact 文件 '{filename}' 读取失败: {exc}"

        if len(file_bytes) > MAX_ARTIFACT_FILE_SIZE:
            return None, (
                f"artifact 文件 '{filename}' 过大: "
                f"{len(file_bytes)} bytes > {MAX_ARTIFACT_FILE_SIZE} bytes"
            )

        result[filename] = base64.b64encode(file_bytes).decode("ascii")

    return result or None, None


def _resp_json(payload: dict[str, Any]) -> ToolResponse:
    """Helper: wrap a JSON-serializable dict as a single-text-block ToolResponse."""
    return ToolResponse(content=[TextBlock(
        type="text",
        text=json.dumps(payload, ensure_ascii=False),
    )])


def _validate_workspace_path(path: str) -> str | None:
    """Reject paths outside the workspace root or with traversal segments.
    Returns an error string on rejection, None on success."""
    from core.sandbox._common import WORKSPACE as _WS
    from ._paths import canonicalize_ws_path

    if not path or not isinstance(path, str):
        return "path 必须为非空字符串"
    path = canonicalize_ws_path(path)
    if not path.startswith(_WS + "/"):
        return f"path 必须在 {_WS}/ 下: {path}"
    if "/../" in path or path.endswith("/..") or "//" in path:
        return f"path 不允许包含 .. 或 //: {path}"
    return None
