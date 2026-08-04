"""Sandbox session keepalive for long-running workflows.

A worker/reviewer agent can stream model output (thinking, huge tool-call
arguments) for well past the idle-reap threshold without issuing a single
sandbox call; the idle reaper then destroys the session mid-run and the
workspace is wiped (observed live: "reaped idle session=loop-... idle
643s > 600s" — no snapshot either, since idle < snapshot threshold). Callers
start a keepalive task for the phase during which the session must stay
alive and cancel it in their ``finally``.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# Well under both the idle-reap threshold (600s) and the server-side TTL
# renew window (1800s), with margin for a delayed tick.
_KEEPALIVE_INTERVAL_S = 240.0


def start_session_keepalive(session_id: str) -> asyncio.Task:
    """Spawn a task that periodically marks ``session_id`` as active.

    The provider and its ``touch_session`` are resolved once; providers whose
    ``touch_session`` is a no-op (no such session yet) are touched again on the
    next tick — the session may be created later in the phase.
    Cancel the returned task when the phase ends.
    """

    async def _run() -> None:
        from core.sandbox import get_sandbox_provider

        touch = get_sandbox_provider().touch_session
        while True:
            await asyncio.sleep(_KEEPALIVE_INTERVAL_S)
            try:
                await touch(session_id)
            except Exception as exc:  # noqa: BLE001 - keepalive must never kill the caller
                logger.debug("session keepalive touch(%s) failed: %s", session_id, exc)

    return asyncio.create_task(_run())
