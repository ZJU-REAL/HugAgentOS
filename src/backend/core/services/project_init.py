"""Expand explicit project initialization commands into a reviewable agent task."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.orm import Session

from core.auth.permissions_iface import resolve_project_permission
from core.db.models import Project
from core.services.project_instructions import ProjectInstructionsService

INIT_COMMANDS = frozenset({"/init", "/初始化指令"})


def resolve_project_init(db: Session, request, user_id: str) -> str | None:
    """Authorize against the saved chat binding, not a client-provided file path."""
    if request.message.strip() not in INIT_COMMANDS:
        return None
    from core.services.chat_service import ChatService

    project_id = request.project_id
    pair = ChatService(db).get_session_with_access(request.chat_id, user_id)
    if pair is not None:
        chat, access = pair
        if access not in ("admin", "edit"):
            raise HTTPException(403, "只读共享会话不能初始化项目指令")
        if chat.project_id:
            if project_id and project_id != chat.project_id:
                raise HTTPException(409, "当前会话绑定的项目与所选项目不一致")
            project_id = chat.project_id
    if not project_id:
        raise HTTPException(400, "请先选择具体项目，再使用 /init 或 /初始化指令")
    project = db.query(Project).filter(
        Project.project_id == project_id, Project.deleted_at.is_(None)
    ).first()
    if project is None or resolve_project_permission(db, user_id, project) == "none":
        raise HTTPException(404, "项目不存在或你无权访问")
    if resolve_project_permission(db, user_id, project) not in ("admin", "edit"):
        raise HTTPException(403, "需要项目编辑权限才能初始化项目指令")
    if request.agent_id or request.mention_agent_id or request.skill_id or request.plugin_id or request.connector_id:
        raise HTTPException(400, "初始化项目指令请使用普通项目对话，先移除已选择的能力")
    if request.plan_chat or request.batch_chat or request.workflow_chat:
        raise HTTPException(400, "初始化项目指令请先退出计划、批量或工作流模式")
    # Initialization needs the normal exploration tools even when the composer
    # was set to quick lookup. This only changes this request, not the saved mode.
    request.mode_slug = "standard"
    if request.chat_mode == "turbo":
        request.chat_mode = "fast"
    snapshot = ProjectInstructionsService(db).read(project)
    request.project_id = project_id
    if project.kind == "local":
        local = (project.extra_data or {}).get("local") or {}
        slug = local.get("slug")
        if not slug or "/" in slug or "\\" in slug or slug in (".", ".."):
            raise HTTPException(409, "本地项目挂载信息不完整")
        target = f"/workspace/local/{slug}/AGENTS.md"
    else:
        target = "/myspace/AGENTS.md"
    prompt = (Path(__file__).resolve().parents[2] / "prompts" / "project_init.md").read_text(encoding="utf-8")
    context = {
        "project_name": project.name,
        "target_path": target,
        "existing_instructions_source": snapshot["instructions_source"],
        "existing_instructions": snapshot["instructions"],
    }
    return prompt + "\n\n## 当前项目（JSON 数据）\n" + json.dumps(context, ensure_ascii=False)
