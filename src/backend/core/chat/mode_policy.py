"""Per-user chat-mode policy: the default mode and whether the user may switch away from it.

Two permission values decide it (see ``core.auth.capabilities``):

- ``default_chat_mode``: the mode slug a user starts in, set per person, role or team;
  unset everywhere → 标准模式.
- ``can_switch_chat_mode``: when off, the composer's mode menu is locked and every turn
  runs in the default mode, whatever the request carries. The server enforces it here;
  the composer lock is only the visible half.

The effective default is always a mode the user can actually run: a slug that no longer
exists or is disabled, or 智能路由 without router access, degrades to 标准模式, so a
locked user is never stuck on a mode that silently falls back every turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.chat import mode_types
from core.infra.exceptions import BadRequestError
from core.infra.logging import get_logger
from sqlalchemy.orm import Session

logger = get_logger(__name__)

SWITCH_FLAG = "can_switch_chat_mode"


@dataclass(frozen=True)
class ModePolicy:
    default_slug: str
    can_switch: bool


def validate_default_mode(db: Session, slug: Optional[str]) -> Optional[str]:
    """Admin-side check of a default mode before it is stored; ``None``/empty clears it.

    Accepts an enabled official mode or 智能路由. Private modes belong to one person and
    cannot be anybody's assigned default.
    """
    value = (slug or "").strip()
    if not value:
        return None
    if value == mode_types.SLUG_AUTO:
        return value
    from core.db.models.chat_mode import ChatMode

    exists = (
        db.query(ChatMode.id)
        .filter(
            ChatMode.slug == value,
            ChatMode.owner_user_id.is_(None),
            ChatMode.deleted_at.is_(None),
            ChatMode.enabled.is_(True),
        )
        .first()
    )
    if exists is None:
        raise BadRequestError(message=f"默认模式不存在或已停用：{value}")
    return value


def _router_usable(caps: Dict[str, Any]) -> bool:
    from core.config.store.intent_router_options import intent_router_options

    return bool(caps.get("can_use_intent_router")) and intent_router_options().enabled


def effective_default_slug(db: Session, user_id: str, caps: Dict[str, Any]) -> str:
    """The configured default when the user can run it, else 标准模式."""
    wanted = str(caps.get("default_chat_mode") or "").strip()
    if not wanted or wanted == mode_types.SLUG_STANDARD:
        return mode_types.SLUG_STANDARD
    if wanted == mode_types.SLUG_AUTO:
        return wanted if _router_usable(caps) else mode_types.SLUG_STANDARD
    from core.chat.modes import ChatModeService

    visible = {row.slug for row in ChatModeService(db).list_for_user(user_id) if row.owner_user_id is None}
    return wanted if wanted in visible else mode_types.SLUG_STANDARD


def policy_for(db: Session, user_id: str) -> ModePolicy:
    from core.auth.capabilities import resolve_user_capabilities

    caps = resolve_user_capabilities(db, str(user_id))
    return ModePolicy(
        default_slug=effective_default_slug(db, str(user_id), caps),
        can_switch=bool(caps.get(SWITCH_FLAG, True)),
    )


def apply_mode_policy(db: Session, request: Any, user_id: str) -> None:
    """Hold a user without ``can_switch_chat_mode`` to their default mode.

    Rewrites ``request.mode_slug`` in place before the context, the persisted user
    message and the run payload are built from it, so resume and rerun see the same
    mode. Users who may switch keep whatever they sent.
    """
    try:
        policy = policy_for(db, user_id)
    except Exception:  # noqa: BLE001 — a lookup failure must not block the chat
        logger.warning("[chat_mode] mode policy lookup failed user=%s", user_id, exc_info=True)
        return
    if policy.can_switch:
        return
    requested = str(getattr(request, "mode_slug", None) or "").strip()
    if requested != policy.default_slug:
        logger.info(
            "[chat_mode] user=%s may not switch modes; %s → %s",
            user_id, requested or "(none)", policy.default_slug,
        )
    request.mode_slug = policy.default_slug
