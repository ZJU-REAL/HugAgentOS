"""Admit durable Plan generation and execution runs."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import core.config.settings as settings
import core.db.engine as engine
import core.db.models as models
import core.runs.ownership as ownership
import core.runs.runtime as runtime
import core.runs.settings as runs_settings
import orchestration.plans.generate_run as _orchestration_plans_generate_run
from core.db.models import ChatRun
from core.infra.logging import get_logger

logger = get_logger(__name__)


async def start_plan_execute_run(
    *,
    plan_id: str,
    chat_id: str,
    user_id: str,
    enabled_mcp_ids: Optional[List[str]] = None,
    enabled_skill_ids: Optional[List[str]] = None,
    enabled_kb_ids: Optional[List[str]] = None,
    enabled_agent_ids: Optional[List[str]] = None,
    session_messages: List[Dict[str, Any]],
    model_name: Optional[str] = None,
    on_accept=None,
) -> ChatRun:
    """Plan Phase-2 (execute): background task on the same ChatRun infrastructure.

    chat_id must exist in chat_sessions (FK). Caller should run _ensure_plan_session first.
    """
    if not chat_id:
        raise ValueError("chat_id is required to start a plan execute run")

    request_payload = {
        "kind": "plan_execute",
        "plan_id": plan_id,
        "chat_id": chat_id,
        "enabled_mcp_ids": enabled_mcp_ids or [],
        "enabled_skill_ids": enabled_skill_ids or [],
        "enabled_kb_ids": enabled_kb_ids or [],
        "enabled_agent_ids": enabled_agent_ids or [],
        "model_name": model_name,
    }
    effective_model_name = model_name or settings.DEFAULT_CHAT_MODEL_ALIAS
    initial_args = {
        "plan_id": plan_id,
        "enabled_mcp_ids": enabled_mcp_ids,
        "enabled_skill_ids": enabled_skill_ids,
        "enabled_kb_ids": enabled_kb_ids,
        "enabled_agent_ids": enabled_agent_ids,
        "session_messages": session_messages,
        "model_name": effective_model_name,
    }
    run = ownership.create_run_record(
        on_accept=on_accept,
        chat_id=chat_id,
        user_id=user_id,
        request_payload=request_payload,
        recovery_snapshot={
            "kind": "plan_execute",
            "initial_args": initial_args,
            "worker_args": {
                "context": {
                    "mcp_ids": list(enabled_mcp_ids or []),
                    "skill_ids": list(enabled_skill_ids or []),
                    "kb_ids": list(enabled_kb_ids or []),
                    "model_name": effective_model_name,
                }
            },
        },
    )
    from orchestration.pending import dispatch_pending
    await dispatch_pending(run)
    logger.info(
        "plan_run_started",
        run_id=run.run_id,
        plan_id=plan_id,
        chat_id=chat_id,
        user_id=user_id,
    )
    return run


def get_run_by_plan_id(plan_id: str) -> Optional[ChatRun]:
    """Reverse-lookup live run by plan_id (used by plan cancel endpoint)."""
    with engine.SessionLocal() as db:
        return (
            db.query(models.ChatRun)
            .filter(
                models.ChatRun.status.in_(runs_settings.LIVE_STATUSES),
                models.ChatRun.request_payload["plan_id"].astext == plan_id,
            )
            .order_by(models.ChatRun.created_at.desc())
            .first()
        )


async def start_plan_generate_run(
    *,
    chat_id: str,
    user_id: str,
    task_description: str,
    model_name: str = settings.DEFAULT_CHAT_MODEL_ALIAS,
    model_provider_id: Optional[str] = None,
    enabled_mcp_ids: Optional[List[str]] = None,
    enabled_skill_ids: Optional[List[str]] = None,
    enabled_kb_ids: Optional[List[str]] = None,
    enabled_agent_ids: Optional[List[str]] = None,
    session_messages: List[Dict[str, Any]],
    uploaded_files: Optional[List[Dict[str, Any]]] = None,
) -> ChatRun:
    """Plan Phase-1 (generate): background task on the same ChatRun infrastructure."""
    if not chat_id:
        raise ValueError("chat_id is required to start a plan generate run")

    request_payload = {
        "kind": "plan_generate",
        "chat_id": chat_id,
        "task_description": task_description[:500],
        "model_name": model_name,
        **({"model_provider_id": model_provider_id} if model_provider_id else {}),
    }
    recovery_snapshot = {
        "kind": "plan_generate",
        "worker_args": {
            "task_description": task_description,
            "model_name": model_name,
            "model_provider_id": model_provider_id,
            "enabled_mcp_ids": list(enabled_mcp_ids or []),
            "enabled_skill_ids": list(enabled_skill_ids or []),
            "enabled_kb_ids": list(enabled_kb_ids or []),
            "enabled_agent_ids": list(enabled_agent_ids or []),
            "session_messages": list(session_messages or []),
            "uploaded_files": list(uploaded_files or []),
        },
    }
    run = ownership.create_run_record(
        chat_id=chat_id,
        user_id=user_id,
        request_payload=request_payload,
        recovery_snapshot=recovery_snapshot,
    )
    journal_owner = ownership.new_worker_owner(run.run_id)
    runtime.register_run_task(
        run.run_id,
        _orchestration_plans_generate_run._run_plan_generate_workflow(
            run_id=run.run_id,
            chat_id=chat_id,
            user_id=user_id,
            message_id=run.message_id,
            task_description=task_description,
            model_name=model_name,
            model_provider_id=model_provider_id,
            enabled_mcp_ids=enabled_mcp_ids,
            enabled_skill_ids=enabled_skill_ids,
            enabled_kb_ids=enabled_kb_ids,
            enabled_agent_ids=enabled_agent_ids,
            session_messages=session_messages,
            uploaded_files=uploaded_files,
            journal_owner=journal_owner,
        ),
        name=f"plan_gen_run:{run.run_id}",
    )
    logger.info("plan_generate_run_started", run_id=run.run_id, chat_id=chat_id)
    return run
