"""Authentication and error translation for feature routes."""

from fastapi import Depends, HTTPException, Query, Header

from sqlalchemy.orm import Session

from core.auth.backend import UserContext, get_current_user

from core.db.engine import get_db

from core.infra.responses import success_response

from personal_agent import access

from personal_agent.context import AgentContext


def service(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
    chat_id: str | None = Query(None, max_length=64),
):
    access.require(db, user.user_id)
    return AgentContext(db, user.user_id, chat_id=chat_id)


def perform(fn):
    try:
        return success_response(data=fn())
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


def device_token(authorization: str = Header()):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Device authentication required")
    return authorization[7:]
