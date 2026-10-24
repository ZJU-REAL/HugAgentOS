"""Whether the CE bootstrap owner still has to finish web onboarding."""
from __future__ import annotations
from typing import Any, Dict, Optional
from core.config.settings import settings
from core.auth import local_users_constants

def ce_onboarding_required(metadata: Optional[Dict[str, Any]]) -> bool:
    """Return whether the CE bootstrap owner must finish the current Web setup flow."""
    if settings.edition.edition != "ce":
        return False
    meta = metadata or {}
    try:
        completed_version = int(meta.get("onboarding_completed_version") or 0)
    except (TypeError, ValueError):
        completed_version = 0
    return bool(meta.get("onboarding_required")) and completed_version < local_users_constants.CE_ONBOARDING_VERSION
