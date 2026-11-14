"""Server-side check of the sites permission (``sites_enabled``).

Sites used to ride on the lab permission; they now have their own capability bit with the
same layered resolution (personal → role → team → system default, on by default). The
management API and the agent's publish/list callbacks both check it. Public site hosting
(``/site/{slug}``) stays open to visitors.
"""

from __future__ import annotations

from core.auth.backend import UserContext, get_current_user
from core.auth.capabilities import resolve_user_capabilities
from core.db import engine
from core.db.engine import get_db
from core.infra.exceptions import AccessDeniedError
from fastapi import Depends
from sqlalchemy.orm import Session

DENIED_MESSAGE = "站点功能未对当前账号开放"


def sites_enabled(db: Session, user_id: str) -> bool:
    return bool(resolve_user_capabilities(db, str(user_id))["sites_enabled"])


def sites_enabled_for(user_id: str) -> bool:
    """Same check with its own session, for callbacks that run in a thread pool."""
    with engine.SessionLocal() as db:
        return sites_enabled(db, user_id)


def require_sites_permission(
    user: UserContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    if not sites_enabled(db, user.user_id):
        raise AccessDeniedError(DENIED_MESSAGE, reason="sites_enabled_disabled")
