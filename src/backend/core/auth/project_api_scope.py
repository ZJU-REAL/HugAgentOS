"""Fail-closed route boundary for project-only API credentials."""

import re

from fastapi import HTTPException

from core.db.models import Artifact, ChatRun, ChatSession
from core.projects.api import make_project_api_scope, project_session_matches


def enforce_project_api_route(request, db, user) -> None:
    if getattr(user, "api_key_project_id", None) is None:
        return
    path = request.url.path
    if path.startswith("/api/"):
        path = path[4:]
    method = request.method.upper()
    if method == "POST" and path == "/v1/agents/responses":
        return
    download = re.fullmatch(r"/files/([^/]+)(?:/text|/preview)?", path)
    if download and method == "GET":
        artifact = db.get(Artifact, download.group(1))
        if artifact and artifact.deleted_at is None and artifact.user_id == user.user_id:
            session = db.get(ChatSession, artifact.chat_id) if artifact.chat_id else None
            if session and project_session_matches(
                session, make_project_api_scope(user, artifact.chat_id)
            ):
                return
    active = re.fullmatch(r"/v1/chats/([^/]+)/active-run", path)
    if active and method == "GET":
        session = db.get(ChatSession, active.group(1))
        if session and project_session_matches(
            session, make_project_api_scope(user, active.group(1))
        ):
            return
    resume = re.fullmatch(r"/v1/chats/stream/([^/]+)", path)
    cancel = re.fullmatch(r"/v1/chat-runs/([^/]+)/cancel", path)
    match = resume if method == "GET" else cancel if method == "POST" else None
    if match:
        run = db.get(ChatRun, match.group(1))
        if run and run.user_id == user.user_id:
            scope = make_project_api_scope(user, run.chat_id)
            session = db.get(ChatSession, run.chat_id)
            payload = run.request_payload or {}
            if (
                session
                and project_session_matches(session, scope)
                and payload.get("project_api_scope") == scope
                and payload.get("api_key_id") == user.api_key_id
            ):
                return
    raise HTTPException(
        403,
        detail={
            "code": "project_api_scope_denied",
            "message": "此 API Key 仅可调用绑定的项目及其 API 会话",
        },
    )
