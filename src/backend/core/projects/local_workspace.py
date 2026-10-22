"""Resolve an explicit local project scope without changing process-global cwd."""

import os
from pathlib import Path

from fastapi import HTTPException


def project_directory(scope, *, validate=True) -> str:
    raw = str(scope.local_path or "").strip()
    if not raw or not Path(raw).expanduser().is_absolute():
        raise ValueError("本地项目未配置有效的绝对路径，请重新绑定项目文件夹")
    root = Path(raw).expanduser().resolve()
    if validate and (not root.is_dir() or not os.access(root, os.R_OK | os.X_OK)):
        raise ValueError(f"本地项目目录不存在或不可访问，请恢复目录或重新绑定：{root}")
    return str(root)


def validate_current_project(scope, actor, *, write=False):
    """Reject stale scopes after deletion/rebinding; never switch an active run."""
    from core.db.engine import SessionLocal
    from core.projects.source import ProjectSourceService

    with SessionLocal() as db:
        project = ProjectSourceService(db).authorized_project(
            scope.project_id, actor or "", write=write
        )
        raw = ((project.extra_data or {}).get("local") or {}).get("path")
        if project.kind != "local" or not raw:
            raise HTTPException(409, "本地项目绑定已失效，请重新打开项目会话")
        expected = project_directory(scope)
        if str(Path(raw).expanduser().resolve()) != expected:
            raise HTTPException(409, "本地项目路径已变更，请重新打开项目会话")


def child_project_context(runtime, *, builtin=False):
    """Keep local cwd for custom children without inheriting unrelated context."""
    context = runtime.get("project_ctx")
    if builtin or (context and context.get("project_api_scope")):
        return context
    from core.config.local_mode import local_mode_enabled

    if local_mode_enabled() and context and context.get("project_is_local"):
        return {key: value for key, value in context.items() if key.startswith("project_")}
    if context and context.get("project_id") and "project_environment_config" in context:
        # Environment constraints are inherited even when conversation/file
        # context remains isolated for a custom cloud subagent.
        return {
            key: context[key]
            for key in ("project_id", "project_environment_config", "project_environment_effective")
            if key in context
        }
    return None
