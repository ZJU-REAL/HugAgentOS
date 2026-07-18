"""User-facing chat model selection helpers.

The frontend may only send a model provider id.  This module owns the server
side allowlist so callers never trust a raw model name from the browser.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from core.auth.capabilities import resolve_user_capabilities
from core.db.models import ModelProvider, ModelRoleAssignment
from core.services.model_config import ModelConfigService


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


def resolve_effective_chat_model_name(
    provider_id: Optional[str] = None,
    *,
    role_key: str = "main_agent",
    fallback_model_name: Optional[str] = None,
) -> Optional[str]:
    """Return the actual upstream model name used by the chat runtime.

    ``ChatRequest.model_name`` is a legacy frontend mode/alias field and often
    defaults to ``qwen``.  Runtime model selection is DB-driven, so logs and
    billing should persist the resolved provider/role model name instead.
    """
    svc = ModelConfigService.get_instance()
    pid = (provider_id or "").strip()
    if pid:
        selected = svc.resolve_provider(pid)
        if selected:
            return selected.model_name

    role = (role_key or "main_agent").strip() or "main_agent"
    resolved = svc.resolve(role)
    if resolved:
        return resolved.model_name
    if role != "main_agent":
        main = svc.resolve("main_agent")
        if main:
            return main.model_name

    fallback = (fallback_model_name or "").strip()
    return fallback or None


def list_user_selectable_models(db: Session) -> list[dict]:
    """List non-secret active chat model providers for the user dropdown."""
    main_assignment = (
        db.query(ModelRoleAssignment).filter(ModelRoleAssignment.role_key == "main_agent").first()
    )
    default_provider_id = main_assignment.provider_id if main_assignment else None
    rows = (
        db.query(ModelProvider)
        .filter(
            ModelProvider.provider_type == "chat",
            ModelProvider.is_active == True,  # noqa: E712
        )
        .order_by(ModelProvider.display_name.asc(), ModelProvider.created_at.desc())
        .all()
    )
    return [
        {
            "provider_id": row.provider_id,
            "display_name": row.display_name,
            "model_name": row.model_name,
            "provider": getattr(row, "provider", None) or "openai_compatible",
            "is_default": row.provider_id == default_provider_id,
            "supports_reasoning_effort": bool(
                (row.extra_config or {}).get("supports_reasoning_effort")
            ),
        }
        for row in rows
    ]
