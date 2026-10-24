"""Encode durable run events into the existing public SSE wire protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any, AsyncIterator, Callable, Dict, Optional

import core.runs.events.subscription as subscription
import core.runs.runtime as runtime
import core.runs.settings as settings
from core.infra.logging import get_logger

logger = get_logger(__name__)


async def follow_run_as_sse(
    run_id: str,
    *,
    chat_id: str,
    from_offset: int = 0,
    error_event_factory: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> AsyncIterator[str]:
    """Wrap follow_run as SSE wire frames.

    ``error_event_factory(reason)`` lets callers customize the error event shape
    (chat protocol vs plan protocol). Defaults to a chat-style ``{type: error}``.
    """

    def _default_err(reason: str) -> Dict[str, Any]:
        return {"type": "error", "error": reason, "chat_id": chat_id}

    factory = error_event_factory or _default_err

    # Decouple follow_run from the yield cadence via an intermediate queue: it
    # lets wait_for write an SSE comment line to the wire when the stream has
    # been silent longer than _HEARTBEAT_INTERVAL_SEC, serving as nginx /
    # reverse-proxy keepalive. Cancelling queue.get via wait_for is safe
    # (asyncio.Queue handles the cancel race); the underlying follow_run
    # coroutine is never interrupted.
    queue: asyncio.Queue = asyncio.Queue()

    async def _pump() -> None:
        try:
            async for event in subscription.follow_run(run_id, from_offset=from_offset):
                await queue.put(("event", event))
        except runtime.ChatRunNotFound:
            await queue.put(("not_found", None))
        except Exception as exc:  # noqa: BLE001
            await queue.put(("error", exc))
        else:
            await queue.put(("end", None))

    pump_task = asyncio.create_task(_pump(), name=f"sse_pump:{run_id}")
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(
                    queue.get(), timeout=settings.HEARTBEAT_INTERVAL_SEC
                )
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue

            if kind == "event":
                clean = {k: v for k, v in payload.items() if not k.startswith("_")}
                yield f"data: {json.dumps(clean, ensure_ascii=False)}\n\n"
            elif kind == "end":
                break
            elif kind == "not_found":
                yield f"data: {json.dumps(factory('run not found'), ensure_ascii=False)}\n\n"
                break
            elif kind == "error":
                logger.warning(
                    "follow_run_as_sse_failed", run_id=run_id, error=str(payload), exc_info=payload
                )
                yield f"data: {json.dumps(factory('流式响应中断'), ensure_ascii=False)}\n\n"
                break
    finally:
        pump_task.cancel()
        with contextlib.suppress(BaseException):
            await pump_task

    yield "data: [DONE]\n\n"
