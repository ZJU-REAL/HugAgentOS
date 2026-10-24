"""Authorize account model overrides before selecting an active provider."""
from __future__ import annotations
from typing import Optional
from sqlalchemy.orm import Session
from core.auth.capabilities import resolve_user_capabilities
from core.db.models import ModelProvider

class UserModelSelectionError(ValueError):
    """Raised when a requested user-selectable model is not allowed."""


def resolve_user_model_provider_id(
    db: Session,
    provider_id: Optional[str],
    *,
    user_id: Optional[str] = None,
    can_switch_model: Optional[bool] = None,
) -> Optional[str]:
    """Return an allowed provider id for the current request.

    Disabled switch means "ignore the browser-provided override" rather than
    "fail the chat"; enabled switch validates against active chat providers.
    """
    pid = (provider_id or "").strip()
    if not pid:
        return None

    enabled = (
        can_switch_model
        if can_switch_model is not None
        else bool(user_id and user_can_switch_model(db, user_id))
    )
    if not enabled:
        return None

    provider = (
        db.query(ModelProvider)
        .filter(
            ModelProvider.provider_id == pid,
            ModelProvider.provider_type == "chat",
            ModelProvider.is_active == True,  # noqa: E712
        )
        .first()
    )
    if provider is None:
        raise UserModelSelectionError("所选模型不可用，请刷新页面后重试。")
    return pid


def user_can_switch_model(db: Session, user_id: str) -> bool:
    """Return whether the user may switch models in the chat input."""
    return bool(resolve_user_capabilities(db, user_id).get("can_switch_model"))
