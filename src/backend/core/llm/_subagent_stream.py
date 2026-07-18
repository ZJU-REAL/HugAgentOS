"""Sub-agent streaming event side-channel — relays call_subagent's internal
thinking / tool_call / tool_result / content events into the main chat SSE stream in real time.

Background: ``call_subagent`` (core/llm/subagent_tool.py) runs the sub-agent in a
**separate thread + separate event loop** (to avoid anyio cancel-scope cross-task
errors). The sub-agent used to execute one-shot via ``agent.reply()``, swallowing all
intermediate events; the frontend only saw a single "call sub-agent" tool card and the
final summary text, with no visibility into its internal tool calls.

This module provides a per-chat side-channel shaped after the proven ``_myspace_confirm``:
- ``stream()`` (orchestration/streaming.py) calls ``attach`` on startup with its own
  ``event_q`` + the loop it runs on, and ``detach`` on shutdown.
- ``call_subagent`` and its worker thread deliver sub-agent events back to the main
  ``event_q`` via ``push`` — using ``loop.call_soon_threadsafe`` for **cross-thread
  safety**; the main stream loop is ``await event_q.get()``-ing, so an arriving event
  wakes it immediately -> true streaming, no polling delay.

Keyed by chat_id: a chat has only one active run at a time (chat_run serialization),
so keys never collide. When chat_id is missing it falls back to ``_nochat_`` (only for
edge flows without a chat; a tiny chance of concurrent key collision is acceptable).

Storage: in-process per-chat (same constraint as myspace-confirm). In multi-worker
setups a sub-agent is naturally in the same process as its parent run (spawned within
the same run executor), so no cross-process support is needed.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Tuple kind delivered to stream()'s event_q — alongside ("ev"|"err"|"done", ...).
QUEUE_KIND = "subagent"

_NO_CHAT = "_nochat_"


@dataclass
class _Bus:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue


_LOCK = threading.RLock()
_BUSES: Dict[str, _Bus] = {}


def _key(chat_id: Optional[str]) -> str:
    return str(chat_id) if chat_id else _NO_CHAT


def attach(chat_id: Optional[str], loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
    """Register this run's event outlet (event_q + its loop) when stream() starts."""
    with _LOCK:
        _BUSES[_key(chat_id)] = _Bus(loop=loop, queue=queue)


def detach(chat_id: Optional[str]) -> None:
    """Deregister when stream() winds down — prevents late events from writing to a finished run's event_q."""
    with _LOCK:
        _BUSES.pop(_key(chat_id), None)


def push(chat_id: Optional[str], payload: Dict[str, Any]) -> None:
    """Deliver one sub-agent event back to the chat's main stream queue.

    Thread-safe: callable from the worker thread (the sub-agent's separate loop) or
    from a coroutine on the main loop — both go through ``loop.call_soon_threadsafe``,
    with the main loop thread executing ``put_nowait``.

    Silently discarded when there is no matching active stream (run finished /
    never registered) or the loop is already closed; never raises and drags down
    the sub-agent.
    """
    with _LOCK:
        bus = _BUSES.get(_key(chat_id))
    if bus is None:
        return
    try:
        bus.loop.call_soon_threadsafe(bus.queue.put_nowait, (QUEUE_KIND, payload))
    except RuntimeError:
        # Loop already closed (run wind-down race) — just discard.
        pass
    except Exception:  # noqa: BLE001 — a side-channel failure must never affect the sub-agent's main flow
        logger.debug("[subagent-stream] push failed (ignored)", exc_info=True)


def is_active(chat_id: Optional[str]) -> bool:
    """Whether the chat currently has an active stream listening — lets call_subagent decide whether to emit streaming events."""
    with _LOCK:
        return _key(chat_id) in _BUSES
