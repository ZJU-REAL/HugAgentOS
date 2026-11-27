"""Display profile (nickname / real name / phone) for both local and external SSO users.

Local accounts keep these fields on ``local_users``. External SSO users have no
``local_users`` row, so an admin edit from the Config console is stored as an
override in ``users_shadow.metadata["admin_profile"]``. Its ``email`` key, when
present, pins ``users_shadow.email`` so the next SSO login does not overwrite it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

ADMIN_PROFILE_KEY = "admin_profile"
PROFILE_FIELDS = ("nickname", "real_name", "phone")


def admin_profile(meta: Optional[dict]) -> Dict[str, Any]:
    raw = (meta or {}).get(ADMIN_PROFILE_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def profile_fields(shadow: Any, local: Any) -> Dict[str, Optional[str]]:
    """nickname / real_name / phone: local account first, else the admin override."""
    if local is not None:
        return {f: getattr(local, f, None) for f in PROFILE_FIELDS}
    override = admin_profile(getattr(shadow, "extra_data", None) if shadow is not None else None)
    return {f: override.get(f) or None for f in PROFILE_FIELDS}


def email_pinned(meta: Optional[dict]) -> bool:
    """True when an admin set this user's email, so SSO sync must keep it."""
    return "email" in admin_profile(meta)
