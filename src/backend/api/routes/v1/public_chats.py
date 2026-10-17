"""Manage a resource's public URL and bootstrap isolated browser conversations."""

from core.auth.backend import UserContext, get_current_user
from core.auth.public_chat import public_session_matches
from core.db.engine import get_db
from core.db.models import ChatSession, Project, UserAgent
from core.infra.responses import success_response
from core.services import public_chat_service as service
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

router = APIRouter(tags=["Public chat"])


class LinkState(BaseModel):
    enabled: bool


@router.get("/v1/{kind}/{resource_id}/web-link")
def read_link(
    kind: str,
    resource_id: str,
    response: Response,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    return success_response(data=service.get_link(db, user.user_id, kind, resource_id))


@router.post("/v1/{kind}/{resource_id}/web-link")
def create_link(
    kind: str,
    resource_id: str,
    response: Response,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    return success_response(data=service.create_link(db, user.user_id, kind, resource_id))


@router.patch("/v1/{kind}/{resource_id}/web-link")
async def toggle_link(
    kind: str,
    resource_id: str,
    body: LinkState,
    response: Response,
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    data = service.set_enabled(db, user.user_id, kind, resource_id, body.enabled)
    if not body.enabled:
        from core.db.models import ContentBlock
        from core.services.api_key_runs import stop_cancelled_key_workers

        row = db.get(ContentBlock, service.link_id(user.user_id, kind, resource_id))
        await stop_cancelled_key_workers(db, row.payload["key_id"], user.user_id)
    return success_response(data=data)


@router.get("/v1/public-chats/session")
def bootstrap(
    response: Response, user: UserContext = Depends(get_current_user), db: Session = Depends(get_db)
):
    response.headers["Cache-Control"] = "no-store"
    if not user.public_chat_visitor:
        raise HTTPException(403, detail="Visitor credential required")
    resource = (
        db.get(UserAgent, user.api_key_agent_id)
        if user.api_key_agent_id
        else db.get(Project, user.api_key_project_id)
    )
    if not resource:
        raise HTTPException(404, detail="Resource unavailable")
    from core.services.session_operations.sessions import _session_to_dict

    scope_name = "agent_api_scope" if user.api_key_agent_id else "project_api_scope"
    # JSON extraction is supported by both SQLite and Postgres. Bound the result,
    # and still check the complete scope before returning any session metadata.
    rows = (
        db.query(ChatSession)
        .filter(
            ChatSession.user_id == user.user_id,
            ChatSession.deleted_at.is_(None),
            ChatSession.extra_data[scope_name]["api_key_id"].as_string() == user.api_key_id,
            ChatSession.extra_data[scope_name]["public_chat_visitor"].as_string()
            == user.public_chat_visitor,
        )
        .order_by(ChatSession.updated_at.desc())
        .limit(100)
        .all()
    )
    return success_response(
        data={
            "id": user.api_key_agent_id or user.api_key_project_id,
            "kind": "agents" if user.api_key_agent_id else "projects",
            "name": resource.name,
            "description": resource.description or "",
            "suggested_questions": getattr(resource, "suggested_questions", None) or [],
            "visitor_id": service.digest(f"{user.api_key_id}:{user.public_chat_visitor}"),
            "chats": [_session_to_dict(row) for row in rows if public_session_matches(row, user)],
        }
    )
