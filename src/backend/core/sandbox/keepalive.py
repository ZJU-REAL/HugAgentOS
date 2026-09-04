"""Sandbox session keepalive for long-running workflows.

A worker/reviewer agent can stream model output (thinking, huge tool-call
arguments) for well past the idle threshold without issuing a single sandbox
call; the idle reaper then reclaims the session mid-run. Callers start a
keepalive task for the phase during which the session must stay alive and
cancel it in their ``finally``.
"""

from __future__ import annotations

import asyncio
import logging

from core.config.settings import settings

logger = logging.getLogger(__name__)


def _interval_s() -> float:
    """Touch four times per idle window, never faster than the server-side renew
    rate limit (60s)."""
    return max(60.0, settings.sandbox.idle_ttl_s / 4)


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
        interval = _interval_s()
        while True:
            await asyncio.sleep(interval)
            try:
                await touch(session_id)
            except Exception as exc:  # noqa: BLE001 - keepalive must never kill the caller
                logger.debug("session keepalive touch(%s) failed: %s", session_id, exc)

    return asyncio.create_task(_run())
