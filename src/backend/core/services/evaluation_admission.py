"""Authorize an evaluation lease before normal chat admission touches user data."""
from fastapi import HTTPException

from core.config.settings import settings
from core.sandbox import evaluation_binding
from core.sandbox.errors import SandboxError


async def authorize(request, user_id, *, agent_scoped=False):
    if not str(request.chat_id or "").startswith("eval_"):
        return
    if settings.sandbox.provider != "opensandbox":
        raise HTTPException(409, detail="evaluation_requires_opensandbox")
    forbidden = (
        "attachments", "project_id", "referenced_chats",
        "quoted_follow_up", "enabled_mcps", "enabled_skills", "skill_id",
        "enabled_kbs", "connector_id", "plugin_id", "mention_agent_id",
        "mention_name", "enabled_agents",
        "plan_chat", "batch_chat", "workflow_chat", "site_chat",
    )
    if agent_scoped or any(getattr(request, field, None) for field in forbidden):
        raise HTTPException(403, detail="evaluation_personal_context_forbidden")
    try:
        await evaluation_binding.activate(request.chat_id, user_id)
    except SandboxError as exc:
        raise HTTPException(409, detail="evaluation_binding_unavailable") from exc
