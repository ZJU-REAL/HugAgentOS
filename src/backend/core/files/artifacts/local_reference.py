"""Live references to local project and session files."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException

PREFIX = "lpf_"


def is_local_file_ref(file_id: str) -> bool:
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
    from core.sandbox.workspace.resolution import to_physical_path

    return to_physical_path(path, user_id, session_id=session_id, scope=scope)


def _allows_live_access(
    path: str, user_id: str, action: str = "read", chat_id: str | None = None
) -> bool:
    from core.sandbox.permissions.dispatch_models import CURRENT_PERMISSION_TICKET
    from core.sandbox.permissions.account import resolve_work_approval_mode
    from core.sandbox.workspace.paths import WORKSPACE
    from core.sandbox.permissions.evaluator import evaluate_local_path
    from core.harness.registry.local_files import grants_for_gate, policy_for_gate
    import os

    try:
        # Session files belong to their chat; project files to the running tool's lease.
        mode = resolve_work_approval_mode(user_id, chat_id)
    except ValueError:
        return False  # A revoked client-session lease never falls back to the account.
    verdict = evaluate_local_path(
        path,
        intent=action,
        grants=grants_for_gate(),
        policy=policy_for_gate(mode),
        workspace_root=WORKSPACE,
        platform="windows" if os.name == "nt" else "posix",
    )
    ticket = CURRENT_PERMISSION_TICKET.get()
    return verdict.decision == "allow" or bool(ticket and ticket.active and ticket.authorizes_path(path, action))


def reference_project_file(path: str, *, scope, user_id: str, name: str = "") -> dict:
    from core.config.local_mode import local_mode_enabled
    from core.db.engine import SessionLocal
    from core.projects.source import ProjectSourceService
    from core.files.artifacts.store import _record_artifact, _now_iso

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
        from core.sandbox.permissions.dispatch_models import require_local_path_permission

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
    from core.projects.source import ProjectSourceService

    if not local_mode_enabled():
        return None
    meta = item.get("metadata") or {}
    if meta.get("reference_kind") == "session":
        return resolve_session_reference(item)
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
    from core.files.artifacts.store import get_artifact

    if not user_id or not is_local_file_ref(file_id):
        return None
    item = get_artifact(file_id)
    if not item or item["metadata"].get("user_id") != user_id:
        return None
    return item


def overwrite_reference(file_id: str, user_id: str, content: bytes, db) -> dict:
    import os
    import tempfile
    from core.projects.source import ProjectSourceService
    from core.harness.registry.local_files import snapshot

    item = authorized_reference(file_id, user_id)
    if not item:
        raise HTTPException(404, "文件不存在或无权访问")
    if item["metadata"].get("reference_kind") != "session":
        ProjectSourceService(db).authorized_project(item["metadata"]["project_id"], user_id, write=True)
    source = Path(item["path"])
    if not _allows_live_access(
        str(source), user_id, "write", item["metadata"].get("session_id")
    ):
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


def reference_local_file(path: str, *, scope, user_id: str, session_id: str | None, name: str = "") -> dict:
    from core.config.local_mode import local_mode_enabled
    from core.config.settings import settings
    from core.sandbox.workspace.resolution import workspace_directory
    from core.sandbox.permissions.dispatch_models import require_local_path_permission
    from core.files.artifacts.store import _write_record_local, _now_iso

    if not local_mode_enabled():
        raise HTTPException(400, "仅本机模式支持按文件路径交付")
    if not user_id:
        raise HTTPException(401, "交付文件需要登录")
    if is_project_file_path(path, scope):
        return reference_project_file(path, scope=scope, user_id=user_id, name=name)
    source = Path(path).expanduser().resolve()
    root = Path(workspace_directory(session_id)).resolve() if session_id else None
    if root is None or not source.is_relative_to(root):
        raise HTTPException(403, "文件必须位于当前会话工作目录或绑定的本机项目目录内")
    require_local_path_permission(str(source), "read")
    if not source.is_file():
        raise HTTPException(404, "文件已移动或删除")
    size = source.stat().st_size
    if size > settings.sandbox.artifact_max_bytes:
        raise HTTPException(413, "文件超过交付大小限制")
    relative = source.relative_to(root).as_posix()
    identity = f"{user_id}\0session\0{session_id}\0{root}\0{relative}"
    item = {
        "file_id": PREFIX + uuid5(NAMESPACE_URL, identity).hex,
        "name": name.strip() or source.name,
        "mime_type": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
        "size": size,
        "path": str(source),
        "created_at": _now_iso(),
        "metadata": {
            "source": "local_project_reference",
            "reference_kind": "session",
            "user_id": user_id,
            "session_id": session_id,
            "session_root": str(root),
            "relative_path": relative,
        },
    }
    _write_record_local(item)
    return item


def resolve_session_reference(item: dict) -> dict | None:
    from core.sandbox.workspace.resolution import workspace_directory

    meta = item.get("metadata") or {}
    try:
        root = Path(workspace_directory(meta["session_id"])).resolve()
        if str(root) != meta["session_root"]:
            return None
        source = (root / meta["relative_path"]).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            return None
        if not _allows_live_access(str(source), meta["user_id"], chat_id=meta["session_id"]):
            return None
        return {**item, "path": str(source), "size": source.stat().st_size}
    except (HTTPException, KeyError, OSError, ValueError):
        return None
