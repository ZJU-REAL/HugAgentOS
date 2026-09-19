"""References to existing local project files; never stores or copies their bytes."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException

PREFIX = "lpf_"


def is_local_project_ref(file_id: str) -> bool:
    return isinstance(file_id, str) and file_id.startswith(PREFIX)


def _project_root(project) -> Path:
    raw = ((project.extra_data or {}).get("local") or {}).get("path")
    if project.kind != "local" or not raw:
        raise HTTPException(404, "本地项目不存在")
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise HTTPException(404, "本地项目文件夹不存在")
    return root


def project_file_path(path: str, scope, user_id: str | None, session_id: str | None = None) -> str:
    from core.llm.tools._paths import to_physical_path

    source = Path(path).expanduser()
    if not source.is_absolute() and scope and scope.local_path:
        source = Path(scope.local_path).expanduser() / source
    return to_physical_path(str(source), user_id, session_id=session_id)


def _allows_live_access(path: str, user_id: str, action: str = "read") -> bool:
    from core.llm.tool_permissions import CURRENT_PERMISSION_TICKET, resolve_approval_mode
    from core.sandbox._common import WORKSPACE
    from core.sandbox.local_policy import evaluate_local_path
    from core.services.local_grant_service import grants_for_gate, policy_for_gate
    import os

    verdict = evaluate_local_path(
        path,
        intent=action,
        grants=grants_for_gate(),
        policy=policy_for_gate(resolve_approval_mode(None, user_id=user_id)),
        workspace_root=WORKSPACE,
        platform="windows" if os.name == "nt" else "posix",
    )
    ticket = CURRENT_PERMISSION_TICKET.get()
    return verdict.decision == "allow" or bool(ticket and ticket.authorizes_path(path, action))


def reference_project_file(path: str, *, scope, user_id: str, name: str = "") -> dict:
    from core.config.local_mode import local_mode_enabled
    from core.db.engine import SessionLocal
    from core.services.project_source import ProjectSourceService
    from core.artifacts.store import _record_artifact, _now_iso

    if not local_mode_enabled() or not scope or not scope.is_local or not user_id:
        raise HTTPException(400, "仅本机项目支持按文件路径交付")
    with SessionLocal() as db:
        project = ProjectSourceService(db).authorized_project(
            scope.project_id, user_id, write=False
        )
        root = _project_root(project)
        if not scope.local_path or root != Path(scope.local_path).expanduser().resolve():
            raise HTTPException(409, "项目目录已变更，请重新打开会话")
        source = Path(path).expanduser()
        if not source.is_absolute():
            source = root / source
        source = source.resolve()
        if not source.is_relative_to(root):
            raise HTTPException(403, "文件必须位于当前本机项目目录内")
        if not source.is_file():
            raise HTTPException(404, "文件已移动或删除")
        from core.llm.tool_permissions import require_local_path_permission

        require_local_path_permission(str(source), "read")
        relative = source.relative_to(root).as_posix()
        # Identity includes the bound root: rebinding cannot redirect historical cards.
        fid = (
            PREFIX + uuid5(NAMESPACE_URL, f"{user_id}\0{scope.project_id}\0{root}\0{relative}").hex
        )
        item = {
            "file_id": fid,
            "name": name.strip() or source.name,
            "mime_type": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
            "size": source.stat().st_size,
            "path": str(source),
            "created_at": _now_iso(),
            "metadata": {
                "user_id": user_id,
                "source": "local_project_reference",
                "project_id": scope.project_id,
                "project_root": str(root),
                "relative_path": relative,
            },
        }
    _record_artifact(item)
    return item


def resolve_project_reference(item: dict) -> dict | None:
    """Revalidate the live binding and symlink boundary on every read/open/preview."""
    from core.config.local_mode import local_mode_enabled
    from core.db.engine import SessionLocal
    from core.services.project_source import ProjectSourceService

    if not local_mode_enabled():
        return None
    meta = item.get("metadata") or {}
    try:
        with SessionLocal() as db:
            project = ProjectSourceService(db).authorized_project(
                meta["project_id"], meta["user_id"], write=False
            )
            root = _project_root(project)
            if str(root) != meta["project_root"]:
                return None
            source = (root / meta["relative_path"]).resolve()
            if not source.is_relative_to(root) or not source.is_file():
                return None
            if not _allows_live_access(str(source), meta["user_id"]):
                return None
            return {**item, "path": str(source), "size": source.stat().st_size}
    except (HTTPException, KeyError, OSError, ValueError):
        return None


def is_project_file_path(path: str, scope) -> bool:
    if not scope or not scope.is_local or not scope.local_path:
        return False
    root = Path(scope.local_path).expanduser().resolve()
    source = Path(path).expanduser()
    # Retain lexical membership so an escaping link is rejected, not exported.
    return source.absolute().is_relative_to(root) or source.resolve().is_relative_to(root)


def authorized_reference(file_id: str, user_id: str | None) -> dict | None:
    from core.artifacts.store import get_artifact

    if not user_id or not is_local_project_ref(file_id):
        return None
    item = get_artifact(file_id)
    if not item or item["metadata"].get("user_id") != user_id:
        return None
    return item


def overwrite_reference(file_id: str, user_id: str, content: bytes, db) -> dict:
    import os
    import tempfile
    from core.services.project_source import ProjectSourceService
    from core.services.local_snapshot_service import snapshot

    item = authorized_reference(file_id, user_id)
    if not item:
        raise HTTPException(404, "文件不存在或无权访问")
    ProjectSourceService(db).authorized_project(item["metadata"]["project_id"], user_id, write=True)
    source = Path(item["path"])
    if not _allows_live_access(str(source), user_id, "write"):
        raise HTTPException(403, "项目文件夹未授权写入，请在本地权限中调整")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=source.parent, prefix=".canvas-save-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(source.stat().st_mode)
        current = authorized_reference(file_id, user_id)
        if not current or current["path"] != str(source):
            raise HTTPException(409, "文件位置已变更，请重新打开")
        snapshot(str(source))
        os.replace(temporary, source)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {
        "file_id": file_id,
        "name": item["name"],
        "size": len(content),
        "mime_type": item["mime_type"],
        "download_url": f"/files/{file_id}",
    }
