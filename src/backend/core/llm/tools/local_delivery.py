"""Prepare desktop files for delivery without a model-visible export step."""

import hashlib
import mimetypes
import tempfile
from pathlib import Path

from fastapi import HTTPException


def prepare_local_delivery(path: str, *, scope, user_id: str, session_id: str | None) -> dict:
    from core.config.local_mode import local_mode_enabled
    from core.config.settings import settings
    from core.artifacts.local_project import is_project_file_path, reference_project_file
    from core.llm.tool_permissions import require_local_path_permission
    from ._paths import workspace_directory
    from ._tool_helpers import _store_generated_file_path

    if not local_mode_enabled():
        raise HTTPException(400, "仅本机模式支持按文件路径交付")
    if not user_id:
        raise HTTPException(401, "交付文件需要登录")
    if is_project_file_path(path, scope):
        return reference_project_file(path, scope=scope, user_id=user_id)

    source = Path(path).expanduser().resolve()
    if not session_id or not source.is_relative_to(Path(workspace_directory(session_id)).resolve()):
        raise HTTPException(403, "文件必须位于当前会话工作目录或绑定的本机项目目录内")
    require_local_path_permission(str(source), "read")
    if not source.is_file():
        raise HTTPException(404, "文件已移动或删除")
    limit = settings.sandbox.artifact_max_bytes
    mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    # Bound the snapshot while reading, including files that grow after stat().
    with tempfile.TemporaryDirectory(prefix="local-delivery-") as directory:
        snapshot = Path(directory) / source.name
        size = 0
        digest = hashlib.sha256()
        with source.open("rb") as reader, snapshot.open("wb") as writer:
            while chunk := reader.read(min(1024 * 1024, limit - size + 1)):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"文件超过交付大小限制（{limit} bytes），请拆分后交付")
                writer.write(chunk)
                digest.update(chunk)
        fingerprint = digest.hexdigest()
        from core.llm import workspace
        from core.artifacts.store import get_artifact

        for pinned in workspace.get_pinned():
            previous = get_artifact(pinned["file_id"])
            meta = (previous or {}).get("metadata") or {}
            if (
                meta.get("user_id") == user_id
                and meta.get("src_path") == str(source)
                and meta.get("sha256") == fingerprint
            ):
                return previous
        item = _store_generated_file_path(
            snapshot,
            name=source.name,
            mime_type=mime,
            user_id=user_id,
            source="pin_to_workspace",
            extra_metadata={"src_path": str(source), "sha256": fingerprint},
        )
    if not item:
        raise HTTPException(503, "文件登记失败，请稍后重试")
    return item
