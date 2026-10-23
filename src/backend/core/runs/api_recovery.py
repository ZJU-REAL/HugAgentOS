"""Restore agent API boundaries from independent durable provenance records."""

from __future__ import annotations

from core.db.models import ChatSession
from core.llm.api_runtime.agent import apply_api_scope, parse_api_scope
from core.auth.agent_api import session_matches_scope
from core.projects.api import project_session_matches


def validate_recovery_context(db, run, context, *, user_id: str, chat_id: str) -> dict:
    """A missing or changed API marker must never restore the owner's context.

    This checks independent durable provenance. The journal separately checks
    credential validity and cancellation before execution or finalization.
    """
    if run is None or run.user_id != user_id or run.chat_id != chat_id:
        raise ValueError("Recovery run identity is unavailable or inconsistent")
    if context is not None and not isinstance(context, dict):
        raise ValueError("Recovery context must be an object")
    context = dict(context or {})
    payload = run.request_payload if isinstance(run.request_payload, dict) else {}
    session = db.get(ChatSession, chat_id)
    metadata = (
        session.extra_data if session is not None and isinstance(session.extra_data, dict) else {}
    )
    records = (payload, context, metadata)
    project_marked = any(record.get("project_api_scope") is not None for record in records)
    if project_marked:
        scope = payload.get("project_api_scope")
        if (
            not isinstance(scope, dict)
            or scope.get("version") != 1
            or scope.get("owner_user_id") != user_id
            or scope.get("chat_id") != chat_id
            or not isinstance(scope.get("api_key_id"), str)
            or not scope.get("api_key_id")
            or payload.get("api_key_id") != scope.get("api_key_id")
            or payload.get("project_id") != scope.get("project_id")
            or context.get("project_id") != scope.get("project_id")
            or context.get("project_api_scope") != scope
            or metadata.get("project_api_scope") != scope
            or context.get("user_id") != user_id
            or context.get("chat_id") != chat_id
            or context.get("conversation_id") not in (None, "", chat_id)
            or session is None
            or not project_session_matches(session, scope)
            or any(record.get("agent_api_scope") is not None for record in records)
        ):
            raise ValueError("Project API recovery scope is missing or inconsistent")
        return context
    marked = any("agent_api_scope" in record for record in (payload, context, metadata))
    if not marked:
        return context
    scope = payload.get("agent_api_scope")
    if (
        not isinstance(scope, dict)
        or context.get("agent_api_scope") != scope
        or metadata.get("agent_api_scope") != scope
        or session is None
    ):
        raise ValueError("Agent API recovery scope is missing or inconsistent")
    parse_api_scope(
        scope,
        owner_user_id=user_id,
        chat_id=chat_id,
        agent_id=payload.get("agent_id"),
    )
    if not session_matches_scope(session, scope):
        raise ValueError("Agent API recovery session no longer matches its scope")
    if context.get("user_id") != user_id or context.get("chat_id") != chat_id:
        raise ValueError("Agent API recovery context identity changed")
    for field in ("direct_agent_id", "agent_id"):
        if context.get(field) != scope["agent_id"]:
            raise ValueError("Agent API recovery target changed")
    if context.get("conversation_id") not in (None, "", chat_id):
        raise ValueError("Agent API recovery conversation changed")
    return apply_api_scope(context)
