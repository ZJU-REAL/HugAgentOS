"""Build execution context and persisted user-message metadata."""

from typing import Any, Dict, List, Optional

import api.routes.v1.chats.session_context as chat_session_context
import core.chat.context as chat_context
import core.db.engine as db_engine
from api.schemas import ChatRequest
from core.infra.exceptions import ServiceUnavailableError
from core.infra.logging import get_logger
from core.llm.tool_permissions import normalize_approval_mode
from fastapi import HTTPException
from sqlalchemy.orm import Session

logger = get_logger(__name__)


def _build_ctx(
    request: ChatRequest,
    db_user_id: str,
    enabled_skills,
    enabled_agents,
    enabled_mcps,
    memory_enabled=False,
    memory_write_enabled=False,
    reranker_enabled=False,
    model_provider_id: Optional[str] = None,
    actual_model_name: Optional[str] = None,
    ontology_enabled: bool = False,
    ontology_pack_ids: Optional[List[str]] = None,
    approval_mode: Optional[str] = None,
):
    # Explicit selection is a per-turn addition to the user's default assembly.
    # Authorization was already checked by _resolve_explicit_capability_invocation.
    explicit_skill_ids = [
        item
        for item in [request.skill_id, *request._resolved_skill_ids]
        if isinstance(item, str) and item.strip()
    ]
    if explicit_skill_ids:
        enabled_skills = sorted(set(enabled_skills or []) | set(explicit_skill_ids))
    if request._resolved_mcp_ids:
        enabled_mcps = sorted(set(enabled_mcps or []) | {m for m in request._resolved_mcp_ids if m})
    current_attachments = (
        [a.model_dump() for a in request.attachments] if request.attachments else []
    )
    current_file_ids = {a.get("file_id") for a in current_attachments if a.get("file_id")}

    historical_files = chat_context.collect_historical_attachments(
        chat_id=request.chat_id,
        user_id=db_user_id,
        exclude_file_ids=current_file_ids,
    )

    # When chat_mode is not explicitly given, default to "thinking: medium"
    resolved_chat_mode = request.chat_mode or "medium"
    # Project metadata is edition-owned; the shared chat path consumes only the
    # returned context map and never imports organization models.
    project_id = getattr(request, "project_id", None)
    project_ctx: Dict[str, Any] = {
        "project_id": project_id,
        "project_init": request.message.strip() in ("/init", "/初始化指令"),
        "project_name": None,
        "project_instructions": None,
        "project_folder_name": None,
        "project_folder_kind": None,
        "project_folder_id": None,
        "project_files": None,
        "project_file_count": None,
    }
    if project_id:
        try:
            from core.db.engine import SessionLocal as _Sess
            from core.services.project_scope import build_project_ctx

            with _Sess() as _db:
                resolved_project_ctx = build_project_ctx(_db, project_id)
                if resolved_project_ctx:
                    project_ctx.update(resolved_project_ctx)
                    memory_enabled = bool(project_ctx.pop("_memory_enabled", True))
                    memory_write_enabled = bool(project_ctx.pop("_memory_write_enabled", True))
        except HTTPException:
            raise
        except Exception:
            logger.warning("[chat] project ctx lookup failed for %s", project_id, exc_info=True)

    workspace_id_value = f"project:{project_id}" if project_id else "default"
    memory_scope_user_id_value = project_ctx.pop("memory_scope_user_id", None)

    from core.services.ontology_service import (
        build_ontology_runtime_for_preference,
        disabled_ontology_runtime,
    )

    ontology_runtime: Dict[str, Any] = disabled_ontology_runtime()
    if ontology_enabled:
        try:
            with db_engine.SessionLocal() as _ontology_db:
                from core.services.ontology_policy import user_can_use_ontology_validation

                if user_can_use_ontology_validation(_ontology_db, db_user_id):
                    ontology_enabled, ontology_runtime = build_ontology_runtime_for_preference(
                        enabled=True,
                        task=request.message,
                        db=_ontology_db,
                        pack_ids=ontology_pack_ids or None,
                    )
                else:
                    ontology_enabled = False
        except Exception as exc:  # noqa: BLE001
            logger.exception("[ontology] failed to build runtime policy")
            raise ServiceUnavailableError(
                "本体校验已开启，但运行时策略暂时不可用；为避免绕过校验，本次请求已停止"
            ) from exc

    ctx: Dict[str, Any] = {
        "model_name": actual_model_name or request.model_name,
        "model_provider_id": model_provider_id or "",
        "user_id": db_user_id,
        "chat_id": request.chat_id,
        "workspace_id": workspace_id_value,
        "memory_scope_user_id": memory_scope_user_id_value,
        "enable_thinking": resolved_chat_mode not in ("fast", "turbo"),
        "chat_mode": resolved_chat_mode,
        "mode_slug": request.mode_slug or "",
        "uploaded_files": current_attachments,
        "historical_files": historical_files,
        "memory_enabled": memory_enabled,
        "memory_write_enabled": memory_write_enabled,
        "reranker_enabled": reranker_enabled,
        "approval_mode": normalize_approval_mode(approval_mode),
        "ontology_enabled": ontology_enabled,
        "ontology_runtime": ontology_runtime,
        # Preserve None so downstream (SkillsMiddleware) falls back to catalog defaults.
        # Only call _clean_id_list when there's an actual list to normalize.
        "enabled_skills": (
            chat_session_context._clean_id_list(enabled_skills)
            if enabled_skills is not None
            else None
        ),
        "enabled_agents": (
            chat_session_context._clean_id_list(enabled_agents)
            if enabled_agents is not None
            else None
        ),
        "enabled_mcps": (
            chat_session_context._clean_id_list(enabled_mcps) if enabled_mcps is not None else None
        ),
        "enabled_kbs": (
            chat_session_context._clean_id_list(request.enabled_kbs)
            if request.enabled_kbs is not None
            else None
        ),
        "agent_id": request.agent_id,
        "mention_agent_id": request.mention_agent_id,
        # A persistent child-agent conversation is the only direct route.
        # Per-turn @mentions stay on the normal main-model stream so the model
        # emits a real call_subagent tool call (with thinking and token deltas).
        "direct_agent_id": request.agent_id,
        "direct_agent_source": "dedicated_chat" if request.agent_id else None,
        "skill_id": request.skill_id,
        "skill_name": request.skill_name,
        "skill_ids": request._resolved_skill_ids or None,
        "mcp_ids": request._resolved_mcp_ids or None,
        # Keep the plugin's authoritative component set separate from other
        # explicitly selected capabilities. The runtime uses these fields to
        # prove that the plugin itself was used, not merely some connector that
        # happened to be selected in the same turn.
        "plugin_skill_ids": request._resolved_plugin_skill_ids or None,
        "plugin_mcp_ids": request._resolved_plugin_mcp_ids or None,
        "connector_id": request.connector_id,
        "connector_name": request.connector_name,
        "plugin_name": request.plugin_name,
        "plugin_id": request.plugin_id,
        "plan_chat": request.plan_chat,
        "batch_chat": request.batch_chat,
        "workflow_chat": request.workflow_chat,
        "site_chat": request.site_chat,
        "disable_batch_plan": request.disable_batch_plan,
        **project_ctx,
    }
    return ctx


def _build_user_extra_data(
    request: ChatRequest,
    model_provider_id: Optional[str] = None,
) -> Dict[str, Any]:
    extra: Dict[str, Any] = {"timestamp": chat_context.now_iso()}
    if model_provider_id:
        extra["model_provider_id"] = model_provider_id
    if request.attachments:
        upload_meta = [
            {
                "name": a.name,
                "mime_type": a.mime_type,
                "file_id": a.file_id,
                "download_url": f"/files/{a.file_id}",
            }
            for a in request.attachments
            if a.file_id
        ]
        if upload_meta:
            extra["attachments"] = upload_meta
    if request.quoted_follow_up:
        extra["quoted_follow_up"] = request.quoted_follow_up.model_dump()
    if getattr(request, "_resolved_reference_cards", None):
        extra["referenced_chats"] = list(request._resolved_reference_cards)
    if request.agent_id:
        extra["agent_id"] = request.agent_id
        if getattr(request, "_resolved_agent_profile", None):
            extra["agent_profile"] = request._resolved_agent_profile
    if request.skill_id:
        extra["skill_id"] = request.skill_id
    if request.skill_name:
        extra["skill_name"] = request.skill_name
    if request.connector_id:
        extra["connector_id"] = request.connector_id
    if request.connector_name:
        extra["connector_name"] = request.connector_name
    if request.plugin_name:
        extra["plugin_name"] = request.plugin_name
    if request.plugin_id:
        extra["plugin_id"] = request.plugin_id
    if request.mention_name:
        extra["mention_name"] = request.mention_name
    if request.mention_agent_id:
        extra["mention_agent_id"] = request.mention_agent_id
        if getattr(request, "_resolved_mention_agent_profile", None):
            extra["mention_agent_profile"] = request._resolved_mention_agent_profile
    return extra


def _resolve_reference_block(db: Session, request: ChatRequest, user_id: str) -> str:
    """解析本轮引用的历史会话，返回拼进用户消息的名片文本。

    名片按当前用户的权限现查，并挂回 ``request`` 上，随后与用户消息一起落库——历史重放
    时照这份快照渲染，模型看到的和当时看到的是同一份。
    """
    from core.services.chat_reference_service import render_reference_block, resolve_reference_cards

    cards = resolve_reference_cards(db, user_id, getattr(request, "referenced_chats", None))
    request._resolved_reference_cards = cards
    return render_reference_block(cards)
