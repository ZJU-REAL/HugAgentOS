"""Shared timing and liveness policy for suspended human interactions.

The actual wait registries remain close to their tool protocols, but every
long-lived interaction uses the same maximum wait window. ChatRun and its
Redis event stream consume these constants too, so infrastructure cannot
expire before a legitimate user wait does.
"""

from __future__ import annotations

import os
from typing import Optional


MAX_WAIT_SECONDS = max(
    1,
    int(os.getenv("HUMAN_INTERACTION_MAX_WAIT_SECONDS", "7200")),
)
STREAM_RECOVERY_GRACE_SECONDS = max(
    60,
    int(os.getenv("HUMAN_INTERACTION_STREAM_RECOVERY_GRACE_SECONDS", "1800")),
)
STREAM_TTL_SECONDS = MAX_WAIT_SECONDS + STREAM_RECOVERY_GRACE_SECONDS


def has_pending(chat_id: Optional[str]) -> bool:
    """Whether a live tool in this chat is suspended on a human decision."""

    if not chat_id:
        return False
    from core.llm.tools import _myspace_confirm, user_questions

    return _myspace_confirm.has_pending(chat_id) or user_questions.has_pending(chat_id)


__all__ = [
    "MAX_WAIT_SECONDS",
    "STREAM_RECOVERY_GRACE_SECONDS",
    "STREAM_TTL_SECONDS",
    "has_pending",
]
