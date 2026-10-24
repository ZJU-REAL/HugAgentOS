"""Personal Agent access: deployment switch and owner-scoped account policy.

CE enables the module for existing authenticated users. EE additionally applies
its account/role/team capability. Object ownership and action approval checks
are enforced independently by the feature services in both editions.
"""

from fastapi import HTTPException

from core.auth.capabilities import user_has_capability
from core.config.settings import settings
from core.db.models import UserShadow
from core.harness.registry import is_enabled

NAME = "personal_agent"
CAPABILITY = "can_use_personal_agent"

def module_enabled() -> bool:
    return is_enabled(NAME)

def allowed(db, user_id: str) -> bool:
    if not module_enabled() or not user_id:
        return False
    if not settings.edition.is_ee:
        return db.get(UserShadow, str(user_id)) is not None
    return user_has_capability(db, str(user_id), CAPABILITY)

def visible(meta: dict | None, caps: dict) -> bool:
    """Value serialized to the frontend; same rule as ``allowed`` (super admins included)."""
    return module_enabled() and (not settings.edition.is_ee or bool((meta or {}).get("role") == "super_admin" or caps.get(CAPABILITY)))

def require(db, user_id: str) -> None:
    if not allowed(db, user_id):
        raise HTTPException(403, "Personal Agent is not enabled for this account")
