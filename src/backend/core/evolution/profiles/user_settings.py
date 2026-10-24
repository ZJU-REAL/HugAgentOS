"""Read and update whether a user's episodes feed the evolution loop."""
from __future__ import annotations
from typing import Any, Dict, Optional
from core.db.engine import SessionLocal
import logging
from core.evolution.profiles import user_settings_constants

logger = logging.getLogger(__name__)

def is_contribution_enabled(metadata: Optional[Dict[str, Any]]) -> bool:
    """Whether this user's episodes may feed the evolution loop.

    Reads the single evolution switch (``evolution_prefs.enabled``), so one
    stored value governs both distilling *for* the user and learning *from*
    them. Off by default: consent is collected by the first-run wizard, and a
    user who never enabled evolution contributes nothing. "On" only ever means
    *within the tenant*: cross-tenant sharing is a separate, independently-
    gated decision (see ``federation``).
    """
    from core.evolution.profiles.prefs import PREFS_KEY, normalise

    if not metadata:
        return False
    return bool(normalise(metadata.get(PREFS_KEY)).get("enabled", False))


def privacy_class_for(metadata: Optional[Dict[str, Any]]) -> str:
    """The Episode privacy class implied by the user's setting."""
    return user_settings_constants.PRIVACY_TENANT if is_contribution_enabled(metadata) else user_settings_constants.PRIVACY_PRIVATE


def resolve_for_user(user_id: str) -> Dict[str, Any]:
    """Read the setting for one user. Degrades to the default, never raises."""
    if not user_id:
        return {user_settings_constants.SETTING_KEY: False, "privacy_class": user_settings_constants.PRIVACY_PRIVATE}
    try:
        from core.db.models import UserShadow

        with SessionLocal() as db:
            row = db.query(UserShadow).filter(UserShadow.user_id == user_id).first()
            metadata = (getattr(row, "extra_data", None) or {}) if row else {}
    except Exception as exc:
        logger.debug("[evo-settings] read failed for %s: %s", user_id, exc)
        metadata = {}

    enabled = is_contribution_enabled(metadata)
    return {user_settings_constants.SETTING_KEY: enabled, "privacy_class": privacy_class_for(metadata)}


def update_for_user(user_id: str, enabled: bool) -> Dict[str, Any]:
    """Persist the setting by writing the single evolution switch.

    Kept as an alias so older bundled frontends PATCHing /evolution/settings
    still work; it writes the same ``evolution_prefs.enabled`` the preferences
    panel writes, never a separate value that could drift from it.

    Turning it off does **not** retroactively delete already-contributed
    evidence — that would silently rewrite history other candidates were derived
    from. It stops future episodes from contributing; withdrawing past evidence
    is a separate, explicit action so its consequences stay visible.
    """
    from core.evolution.profiles.prefs import update_prefs

    merged = update_prefs(user_id, {"enabled": bool(enabled)})
    on = bool(merged.get("enabled", False))
    return {
        user_settings_constants.SETTING_KEY: on,
        "privacy_class": user_settings_constants.PRIVACY_TENANT if on else user_settings_constants.PRIVACY_PRIVATE,
        "note": "仅影响此后新产生的证据；已贡献的证据需单独撤回",
    }
