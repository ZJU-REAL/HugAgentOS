"""How long a turn waits for memory retrieval, per the active agent profile."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _profile_memory_budget_ms(context: Dict[str, Any]) -> Optional[int]:
    """How long this task type waits for memory, per the active profile.

    Returns ``None`` when nothing is published, which leaves the caller on the
    deployment default — the built-in profile carries that same value, so this
    is a no-op until a profile deliberately changes it.

    Never raises: retrieval sits on the user's critical path, and an orchestration
    lookup must not be able to fail a turn.
    """
    try:
        from core.auth.tenancy import tenant_of
        from core.evolution.profiles.profile_selection import load_active_profile

        user_id = str(context.get("user_id") or "")
        profile = load_active_profile(
            task_type=str(context.get("chat_mode") or "chat"),
            user_id=user_id,
            tenant_id=tenant_of(user_id),
        )
        return profile.memory_budget_ms
    except Exception as exc:  # noqa: BLE001
        logger.debug("[memory] profile budget unavailable: %s", exc)
        return None
