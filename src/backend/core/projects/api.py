"""Project credential admission and API conversation provenance."""

import re

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from core.db.models import ChatSession, Project


def require_project_key_access(db, user_id: str, project_id: str):
    from core.auth.edition import resolve_project_permission

    project = db.get(Project, project_id)
    if project is None or project.deleted_at is not None:
        raise HTTPException(404, detail={"code": "project_not_found"})
    if resolve_project_permission(db, user_id, project) not in ("admin", "edit"):
        raise HTTPException(403, detail={"code": "project_edit_required"})
    return project


def make_project_api_scope(user, chat_id: str) -> dict:
    scope = {
        "version": 1,
        "owner_user_id": str(user.user_id),
        "api_key_id": str(user.api_key_id),
        "project_id": str(user.api_key_project_id),
        "chat_id": chat_id,
    }

    if getattr(user, "public_chat_visitor", None):
        scope["public_chat_visitor"] = user.public_chat_visitor
    return scope


def project_session_matches(session, scope: dict) -> bool:
    metadata = session.extra_data if isinstance(session.extra_data, dict) else {}
    return (
        session.deleted_at is None
        and session.user_id == scope["owner_user_id"]
        and session.project_id == scope["project_id"]
        and session.channel_id is None
        and metadata.get("project_api_scope") == scope
    )


def prepare_project_api_request(db, user, request):
    project_id = getattr(user, "api_key_project_id", None)
    if project_id is None:
        return request
    if getattr(user, "api_key_agent_id", None) is not None:
        raise HTTPException(403, detail={"code": "invalid_api_scope"})
    if request.project_id and request.project_id != project_id:
        raise HTTPException(403, detail={"code": "project_key_target_mismatch"})
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", request.chat_id):
        raise HTTPException(422, detail={"code": "invalid_api_chat_id"})
    # Reference cards are request context, so their project must match before
    # the reference resolver reads titles or message excerpts.
    for reference in request.referenced_chats:
        referenced = db.get(ChatSession, reference.chat_id)
        if (
            referenced is None
            or referenced.deleted_at is not None
            or referenced.project_id != project_id
        ):
            raise HTTPException(403, detail={"code": "project_reference_mismatch"})
    project = require_project_key_access(db, str(user.user_id), str(project_id))
    request.project_id = project_id
    scope = make_project_api_scope(user, request.chat_id)
    session = db.get(ChatSession, request.chat_id)
    if session is None:
        try:
            with db.begin_nested():
                session = ChatSession(
                    chat_id=request.chat_id,
                    user_id=scope["owner_user_id"],
                    project_id=project_id,
                    title=f"{project.name} · API"[:500],
                    extra_data={"project_api_scope": scope},
                )
                db.add(session)
                db.flush()
            db.commit()
        except IntegrityError:
            db.expire_all()
            session = db.get(ChatSession, request.chat_id)
    if session is None or not project_session_matches(session, scope):
        raise HTTPException(403, detail={"code": "project_api_session_mismatch"})
    return request
