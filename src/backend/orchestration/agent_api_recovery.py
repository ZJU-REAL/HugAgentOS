"""Restore agent API boundaries from independent durable provenance records."""

from __future__ import annotations

from core.db.models import ChatSession
from core.llm.agent_api_runtime import apply_api_scope, parse_api_scope
from core.services.agent_api_service import session_matches_scope


def validate_recovery_context(db, run, context, *, user_id: str, chat_id: str) -> dict:
    """A missing or changed API marker must never restore the owner's context.

    Accepted credentials are not reauthenticated here: revoking a key prevents
    new requests but does not cancel a run already accepted into the journal.
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
