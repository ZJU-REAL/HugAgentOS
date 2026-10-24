"""User-facing chat model selection helpers.

The frontend may only send a model provider id.  This module owns the server
side allowlist so callers never trust a raw model name from the browser.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from core.db.models import ModelProvider, ModelRoleAssignment
from core.models.config import ModelConfigService
from core.models.reasoning_effort import reasoning_capabilities








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
            **reasoning_capabilities(row.extra_config),
            # Real context window (tokens) so the frontend can show accurate
            # context-usage instead of guessing from the model name. May be 0 /
            # missing when an admin has not filled it in — the caller falls back.
            "context_length": int((row.extra_config or {}).get("context_length") or 0),
        }
        for row in rows
    ]
