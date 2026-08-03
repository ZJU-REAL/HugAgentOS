"""Join the two background tasks a turn produces into one settlement.

A turn's card cannot be settled at any single point in the response path,
because the two things it reports finish at different times and neither is on
the critical path:

* **evidence assembly** (episode, skills opened, active profile) runs right
  after the stream closes and is fast;
* **the memory write pipeline** runs extractors against the LLM and finishes
  seconds later — and it is the only component that knows how many memories were
  actually written.

The previous version settled at the first of those and filled the memory number
in from the *retrieval* count, because that was the only number available at
that moment. That is a different quantity entirely: it counted what this turn
read, and presented it as what this turn learned. Every turn with a memory hit
therefore rendered "新增 N 条记忆" without a single write having happened.

So settlement waits for both parties. Whoever arrives last triggers it; a
watchdog settles anyway if the memory pipeline never reports, so a turn can
never be left pending forever.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# How long to wait for the memory pipeline before settling without it. Extractors
# call an LLM with their own 30s timeout, so this sits just beyond that: settling
# early would report zero writes for a turn that was about to write.
MEMORY_WAIT_SECONDS = 45.0

_lock = threading.Lock()


@dataclass
class _PendingTurn:
    message_id: str
    episode_id: str = ""
    # Set once evidence assembly has registered; until then a memory report has
    # nowhere to land and is parked.
    registered: bool = False
    expects_memory: bool = False
    memory_reported: bool = False
    memory_entries: List[Dict[str, Any]] = field(default_factory=list)
    memory_failed: bool = False
    timer: Optional[threading.Timer] = None


_pending: Dict[str, _PendingTurn] = {}

# Turns already settled. A late duplicate — the watchdog fired and the pipeline
# reported afterwards — must not settle a second time: the second pass would
# start from an empty entry and overwrite a good summary with an empty one.
# Bounded, because this is process-lifetime state on a hot path.
_settled: "OrderedDict[str, None]" = OrderedDict()
_SETTLED_MEMORY = 512


def _remember_settled(message_id: str) -> None:
    _settled[message_id] = None
    while len(_settled) > _SETTLED_MEMORY:
        _settled.popitem(last=False)


def _entry(message_id: str) -> _PendingTurn:
    entry = _pending.get(message_id)
    if entry is None:
        entry = _PendingTurn(message_id=message_id)
        _pending[message_id] = entry
    return entry


def register_turn(
    *,
    message_id: str,
    episode_id: str = "",
    expects_memory: bool = False,
) -> None:
    """Evidence assembly finished; settle now, or wait for the memory pipeline."""
    if not message_id:
        return

    with _lock:
        if message_id in _settled:
            return
        entry = _entry(message_id)
        entry.episode_id = episode_id or entry.episode_id
        entry.expects_memory = expects_memory
        entry.registered = True
        ready = entry.memory_reported or not expects_memory
        if not ready and entry.timer is None:
            entry.timer = threading.Timer(
                MEMORY_WAIT_SECONDS, lambda: _settle(message_id, timed_out=True)
            )
            entry.timer.daemon = True
            entry.timer.start()

    if ready:
        _settle(message_id)


def report_memory_writes(
    message_id: str,
    *,
    items: Optional[List[Dict[str, Any]]] = None,
    failed: bool = False,
) -> None:
    """The write pipeline reporting the memories it actually persisted.

    Called on every exit path — including the gates that skip writing entirely —
    so a turn is never left waiting on a report that will not come.
    """
    if not message_id:
        return

    with _lock:
        if message_id in _settled:
            return
        entry = _entry(message_id)
        entry.memory_reported = True
        entry.memory_entries = list(items or [])
        entry.memory_failed = bool(failed)
        ready = entry.registered
        if not ready and entry.timer is None:
            # Evidence assembly should have registered by now; if it failed it
            # never will. Settle on the memory facts alone rather than parking
            # this entry forever — an unregistered turn would otherwise leak.
            entry.timer = threading.Timer(
                MEMORY_WAIT_SECONDS, lambda: _settle(message_id, timed_out=True)
            )
            entry.timer.daemon = True
            entry.timer.start()

    if ready:
        _settle(message_id)


def _settle(message_id: str, *, timed_out: bool = False) -> None:
    """Build the summary and persist it. Never raises — this runs post-response."""
    with _lock:
        entry = _pending.pop(message_id, None)
        if entry is None or message_id in _settled:
            return  # already settled
        if entry.timer is not None:
            entry.timer.cancel()
        _remember_settled(message_id)

    if timed_out and entry.expects_memory:
        # Not an error state: the pipeline may legitimately still be running.
        # The turn settles on what is known, and a card for a turn that wrote
        # nothing simply does not render.
        logger.debug(
            "[evolution] settling %s without a memory report (waited %.0fs)",
            message_id,
            MEMORY_WAIT_SECONDS,
        )

    try:
        from core.evolution.settlement import settle_turn
        from core.evolution.settlement_store import persist_summary

        summary = settle_turn(
            message_id=message_id,
            episode_id=entry.episode_id,
            memory_entries=entry.memory_entries,
            memory_failed=entry.memory_failed,
            memory_enabled=entry.expects_memory,
        )
        persist_summary(message_id, summary)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[evolution] settling %s failed: %s", message_id, exc)


def reset_for_tests() -> None:
    with _lock:
        for entry in _pending.values():
            if entry.timer is not None:
                entry.timer.cancel()
        _pending.clear()
        _settled.clear()
