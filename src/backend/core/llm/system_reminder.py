"""Out-of-band system reminder (<system-reminder>) channel.

Borrows Claude Code's ``wrapInSystemReminder`` pattern: every "system wants to remind the model"
string is uniformly wrapped in ``<system-reminder>...</system-reminder>`` and appended to
``agent.memory`` as a ``user`` role message (**key: the role must be user, not system** —
a mid-conversation system role triggers the model's defensive behavior and causes ReAct to exit).

Callers must do their own within-turn idempotent dedup (e.g. pin_hint dedups itself using
ContextVar state). This module does not provide cross-hook dedup — that has been proven to be
over-engineering: the previous tag mechanism made pin_hint + goal_anchor mask each other, which
was instead hard to debug.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

REMINDER_OPEN = "<system-reminder>"
REMINDER_CLOSE = "</system-reminder>"


def wrap_reminder(content: str) -> str:
    """Wrap a piece of text in ``<system-reminder>...</system-reminder>``."""
    return f"{REMINDER_OPEN}\n{content.strip()}\n{REMINDER_CLOSE}"


async def inject_reminder(agent: Any, content: str) -> bool:
    """Wrap ``content`` as a system-reminder and append it to ``agent.state.context``.

    Uses the ``user`` role — see the module docstring for details.
    AgentScope 2.0: the memory module has been removed; the context is
    ``agent.state.context: list[Msg]``, and Msg.content must be a list of blocks.

    Returns:
        True on success; False if content is empty or the write raised.
    """
    text = (content or "").strip()
    if not text:
        return False

    from agentscope.message import Msg, TextBlock

    msg = Msg(
        name="user",
        content=[TextBlock(type="text", text=wrap_reminder(text))],
        role="user",
    )
    try:
        agent.state.context.append(msg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[system_reminder] context.append failed: %s", exc, exc_info=True)
        return False
    return True
