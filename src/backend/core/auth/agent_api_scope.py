"""Route and run authorization for resource-scoped API credentials."""

import re

from fastapi import HTTPException

from core.db.models import Artifact, ChatRun, ChatSession
from core.services.agent_api_service import make_agent_api_scope, session_matches_scope


def enforce_agent_api_route(request, db, user) -> None:
    if getattr(user, "api_key_agent_id", None) is None:
        return
    path = request.url.path
    if path.startswith("/api/"):
        path = path[4:]
    method = request.method.upper()
    if method == "POST" and path == "/v1/agents/responses":
        return
    download = re.fullmatch(r"/files/([^/]+)", path)
    if download and method == "GET":
        artifact = db.get(Artifact, download.group(1))
        if (
            artifact
            and artifact.deleted_at is None
            and artifact.user_id == user.user_id
            and artifact.chat_id
        ):
            scope = make_agent_api_scope(user, artifact.chat_id)
            session = db.get(ChatSession, artifact.chat_id)
            metadata = artifact.extra_data if isinstance(artifact.extra_data, dict) else {}
            if (
                metadata.get("agent_api_key_id") == user.api_key_id
                and session
                and session_matches_scope(session, scope)
            ):
                return
    resume = re.fullmatch(r"/v1/chats/stream/([^/]+)", path)
    cancel = re.fullmatch(r"/v1/chat-runs/([^/]+)/cancel", path)
    active = re.fullmatch(r"/v1/chats/([^/]+)/active-run", path)
    if active and method == "GET":
        scope = make_agent_api_scope(user, active.group(1))
        session = db.get(ChatSession, scope["chat_id"])
        if session and session_matches_scope(session, scope):
            return
    match = resume if method == "GET" else cancel if method == "POST" else None
    if match:
        run = db.get(ChatRun, match.group(1))
        if run is not None:
            scope = make_agent_api_scope(user, run.chat_id)
            payload = run.request_payload if isinstance(run.request_payload, dict) else {}
            session = db.get(ChatSession, run.chat_id)
            if (
                run.user_id == user.user_id
                and payload.get("agent_api_scope") == scope
                and session
                and session_matches_scope(session, scope)
            ):
                return
    raise HTTPException(
        status_code=403,
        detail={
            "code": "agent_api_scope_denied",
            "message": "此 API Key 仅可调用绑定的子智能体及其 API 会话",
        },
    )
