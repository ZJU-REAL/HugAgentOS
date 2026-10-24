"""Explicit visitor credentials take precedence over any account cookie."""

import re

from core.db.models import Artifact, ChatRun, ChatSession
from core.auth.public_identity import resolve_public_user
from fastapi import HTTPException


def public_session_matches(session, user):
    from core.auth.agent_api import make_agent_api_scope, session_matches_scope
    from core.projects.api import make_project_api_scope, project_session_matches

    if not session:
        return False
    if user.api_key_agent_id:
        return session_matches_scope(session, make_agent_api_scope(user, session.chat_id))
    return project_session_matches(session, make_project_api_scope(user, session.chat_id))


def authenticate_public_chat(request, db):
    user = resolve_public_user(
        db, request.headers.get("x-public-chat", ""), request.headers.get("x-public-visitor", "")
    )
    path = request.url.path.removeprefix("/api")
    if request.method == "GET" and path == "/v1/public-chats/session":
        return user
    if request.method == "GET" and path == "/v1/models/capabilities":
        return user
    if (request.method, path) in {("GET", "/v1/audio/status"), ("POST", "/v1/audio/transcriptions")}:
        return user
    if path == "/v1/tool-approval" and request.method in ("GET", "PUT"):
        return user
    if request.method == "POST" and path == "/v1/file/upload":
        # The upload route validates the multipart chat target before storing bytes.
        return user
    interaction = re.fullmatch(
        r"/v1/chats/([^/]+)/(pending-confirm|pending-user-questions|file-confirm|user-questions/[^/]+/(?:answer|cancel))", path
    )
    if interaction and public_session_matches(db.get(ChatSession, interaction.group(1)), user):
        action = interaction.group(2)
        if (request.method == "GET" and action.startswith("pending-")) or (
            request.method == "POST" and (action == "file-confirm" or action.startswith("user-questions/"))
        ):
            return user
    download = re.fullmatch(r"/files/([^/]+)(?:/text|/preview)?", path)
    if download and request.method == "GET":
        artifact = db.get(Artifact, download.group(1))
        session = db.get(ChatSession, artifact.chat_id) if artifact and artifact.chat_id else None
        if artifact and artifact.deleted_at is None and public_session_matches(session, user):
            return user
    # History is available only to the visitor whose hash is part of the exact
    # immutable API scope. Never enable the owner's general session listing.
    history = re.fullmatch(
        r"/v1/chats/([^/]+)(?:/messages(?:/[^/]+/tool-calls/[^/]+)?|/context-usage)?", path
    )
    if (
        history
        and request.method == "GET"
        and public_session_matches(db.get(ChatSession, history.group(1)), user)
    ):
        return user
    subscription = re.fullmatch(r"/v1/chats/stream/([^/]+)/subscription", path)
    if subscription and request.method == "GET":
        run = db.get(ChatRun, subscription.group(1))
        if run and public_session_matches(db.get(ChatSession, run.chat_id), user):
            return user
        raise HTTPException(403, detail="Visitor run mismatch")
    if user.api_key_agent_id:
        from core.auth.agent_api_scope import enforce_agent_api_route

        enforce_agent_api_route(request, db, user)
    elif user.api_key_project_id:
        from core.auth.project_api_scope import enforce_project_api_route

        enforce_project_api_route(request, db, user)
    else:
        raise HTTPException(403, detail="Public chat requires resource scope")
    return user
