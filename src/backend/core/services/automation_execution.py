"""Execution location and project binding shared by API, MCP and scheduler."""
from __future__ import annotations

import hashlib
import os
import platform
import re
import uuid
from pathlib import Path


def instance_location():
    from core.config.local_mode import local_mode_enabled
    return "local" if local_mode_enabled() else "cloud"


def device_identity():
    name = platform.node() or "This computer"
    identity = hashlib.sha256(f"{name}:{uuid.getnode()}".encode()).hexdigest()[:32]
    return {"device_id": identity, "device_name": name}


def references_host_path(prompt):
    return bool(re.search(r"(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/]|/Users/|/home/|/mnt/)", prompt or ""))


def execution_metadata(db, user_id, *, location=None, project_id=None, prompt=""):
    from core.db.models import Project
    from core.auth.permissions_iface import resolve_project_permission

    actual = instance_location()
    location = location or actual
    if location not in {"local", "cloud"} or location != actual:
        raise ValueError("执行位置与当前服务不匹配，请选择对应的本机或云端服务")
    if location == "cloud" and references_host_path(prompt):
        raise ValueError("云端任务不能直接访问本机目录，请选择本机执行并绑定项目")
    data = {"execution_location": location}
    if location == "local":
        data.update(device_identity())
    if not project_id:
        if location == "local" and references_host_path(prompt):
            raise ValueError("读取本机目录的定时任务必须绑定本机项目")
        return data
    project = db.get(Project, project_id)
    if project is None or project.deleted_at is not None:
        raise ValueError("项目不存在或无权访问")
    # Local directories are exclusively owned; cloud projects retain the
    # edition's existing personal/team permission semantics.
    if project.kind == "local":
        permitted = project.owner_user_id == user_id
    else:
        permitted = resolve_project_permission(db, user_id, project) in {"view", "edit", "admin"}
    if not permitted:
        raise ValueError("项目不存在或无权访问")
    if (project.kind == "local") != (location == "local"):
        raise ValueError("项目与执行位置不匹配，本机目录必须在本机执行")
    data.update(project_id=project.project_id, project_name=project.name)
    if project.kind == "local":
        local = (project.extra_data or {}).get("local") or {}
        raw = local.get("path")
        if not raw or not Path(raw).is_absolute() or not Path(raw).is_dir():
            raise ValueError("项目目录不存在或不可访问")
        data["project_local_path"] = str(Path(raw).resolve())
        data["project_local_slug"] = local.get("slug")
    return data


def task_execution_context(db, task):
    from core.services.project_scope import build_project_ctx

    saved = dict(task.extra_data or {})
    # Legacy tasks belong to the backend where they were stored.
    location = saved.get("execution_location") or instance_location()
    current = execution_metadata(db, task.user_id, location=location,
                                 project_id=saved.get("project_id"))
    if location == "local" and saved.get("device_id") and saved["device_id"] != current["device_id"]:
        raise ValueError("本机任务属于另一台设备")
    if saved.get("project_local_path") != current.get("project_local_path"):
        raise ValueError("项目目录绑定已变更，请重新创建定时任务")
    if not saved.get("project_id"):
        return {}
    if location == "local":
        from core.sandbox.local_mount import ensure_local_project_link
        if saved.get("project_local_slug") != current.get("project_local_slug"):
            raise ValueError("项目目录绑定已变更，请重新创建定时任务")
        ensure_local_project_link(current.get("project_local_slug"), current["project_local_path"])
    context = build_project_ctx(db, saved["project_id"])
    if not context:
        raise ValueError("项目不存在或无权访问")
    context.pop("_memory_enabled", None)
    context.pop("_memory_write_enabled", None)
    return context
