"""Trace-event vocabulary for the evolution evidence plane (GCE ticket 04).

A run's traces are already recorded — tool calls, skill invocations, sub-agent
calls, ontology gate decisions all have their own tables.  What was missing is a
single causal view of one task, and a stable place to record the things no
existing table captures (which memories were injected, which skills were
*considered but not selected*, when the loop stalled).

This module defines the event vocabulary and a sink that appends without ever
blocking the answer.  The rule that shapes everything here: **the response path
only binds versions and appends events**; assembly, joining and attribution all
happen after the stream closes.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Event types ──────────────────────────────────────────────────────────────
# Kept as plain strings so the value persisted in the database stays readable in
# a psql session years from now, when the enum has moved on.

EV_RUN_STARTED = "run.started"
EV_RUN_FINISHED = "run.finished"
EV_MEMORY_RETRIEVED = "memory.retrieved"
EV_MEMORY_INJECTED = "memory.injected"
EV_MEMORY_WRITTEN = "memory.written"
EV_SKILL_SELECTED = "skill.selected"
# The model opened a skill's SKILL.md for itself. This is the only evidence that
# a skill was *used* rather than merely offered: selection puts a name and one
# line of description in front of the model, and a skill nobody ever opens is
# indistinguishable — from selection alone — from one that gets opened every
# time. Retirement and merge decisions read this, never selection.
EV_SKILL_OPENED = "skill.opened"
EV_SKILL_INVOKED = "skill.invoked"
EV_TOOL_CALLED = "tool.called"
EV_SUBAGENT_CALLED = "subagent.called"
EV_ONTOLOGY_GATE = "ontology.gate"
EV_LOOP_PROGRESS = "loop.progress"
EV_LOOP_STAGNATION = "loop.stagnation"
EV_LOOP_INTERVENTION = "loop.intervention"
EV_OUTCOME_RECORDED = "outcome.recorded"

ALL_EVENT_TYPES = (
    EV_RUN_STARTED,
    EV_RUN_FINISHED,
    EV_MEMORY_RETRIEVED,
    EV_MEMORY_INJECTED,
    EV_MEMORY_WRITTEN,
    EV_SKILL_SELECTED,
    EV_SKILL_OPENED,
    EV_SKILL_INVOKED,
    EV_TOOL_CALLED,
    EV_SUBAGENT_CALLED,
    EV_ONTOLOGY_GATE,
    EV_LOOP_PROGRESS,
    EV_LOOP_STAGNATION,
    EV_LOOP_INTERVENTION,
    EV_OUTCOME_RECORDED,
)

ACTOR_SYSTEM = "system"
ACTOR_AGENT = "agent"
ACTOR_USER = "user"
ACTOR_TOOL = "tool"

# Anything larger than this is a result body, not evidence. Oversized payloads
# are replaced by a reference so the event table stays queryable — the full
# content already lives in its own table.
_MAX_INLINE_PAYLOAD_CHARS = 4000


@dataclass
class TraceEvent:
    """One append-only observation about a run."""

    event_type: str
    # Join key. Assistant message ids are pre-allocated and already present on
    # every existing log table, which is why they — not a new run_id column —
    # are what ties this to history.
    message_id: str = ""
    run_id: str = ""
    chat_id: str = ""
    user_id: str = ""
    seq: int = 0
    actor_type: str = ACTOR_SYSTEM
    actor_id: str = ""
    asset_kind: str = ""
    asset_version_id: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    payload_ref: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    event_id: str = field(default_factory=lambda: f"ev_{uuid.uuid4().hex[:20]}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "message_id": self.message_id,
            "run_id": self.run_id,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "seq": self.seq,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "asset_kind": self.asset_kind,
            "asset_version_id": self.asset_version_id,
            "payload": self.payload,
            "payload_ref": self.payload_ref,
            "created_at": self.created_at.isoformat(),
        }


def _shrink(payload: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
    """Keep events small; hand back a reference when the payload is a body."""
    try:
        import json

        encoded = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return {"_unserializable": True}, ""
    if len(encoded) <= _MAX_INLINE_PAYLOAD_CHARS:
        return payload, ""
    ref = f"oversized:{len(encoded)}"
    return {"_truncated": True, "_bytes": len(encoded)}, ref


class TraceSink:
    """Collects a run's events, then flushes them in one write.

    Buffering matters for two reasons: it keeps per-event database round-trips
    off the response path, and it makes ``seq`` assignment trivially correct
    without a shared counter.  A flush failure is logged and swallowed —
    evidence is worth having, but never at the cost of the user's answer.
    """

    def __init__(
        self,
        *,
        run_id: str = "",
        message_id: str = "",
        chat_id: str = "",
        user_id: str = "",
    ) -> None:
        self.run_id = run_id
        self.message_id = message_id
        self.chat_id = chat_id
        self.user_id = user_id
        self._events: List[TraceEvent] = []
        self._seq = 0
        self._flushed = False

    def __len__(self) -> int:
        return len(self._events)

    @property
    def events(self) -> List[TraceEvent]:
        return list(self._events)

    def append(
        self,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        actor_type: str = ACTOR_SYSTEM,
        actor_id: str = "",
        asset_kind: str = "",
        asset_version_id: str = "",
    ) -> Optional[TraceEvent]:
        """Record one observation. Never raises."""
        try:
            body, ref = _shrink(dict(payload or {}))
            self._seq += 1
            event = TraceEvent(
                event_type=event_type,
                message_id=self.message_id,
                run_id=self.run_id,
                chat_id=self.chat_id,
                user_id=self.user_id,
                seq=self._seq,
                actor_type=actor_type,
                actor_id=actor_id,
                asset_kind=asset_kind,
                asset_version_id=asset_version_id,
                payload=body,
                payload_ref=ref,
            )
            self._events.append(event)
            return event
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[trace] append failed, dropping event: %s", exc)
            return None

    def flush(self) -> int:
        """Persist buffered events. Idempotent; safe to call more than once."""
        if self._flushed or not self._events:
            return 0
        try:
            from core.evolution.trace_store import persist_events

            written = persist_events(self._events)
            self._flushed = True
            return written
        except Exception as exc:
            # Losing evidence is bad; failing the turn over it is worse.
            logger.warning("[trace] flush failed for run=%s: %s", self.run_id, exc)
            return 0


_NULL_SINK = TraceSink()


def null_sink() -> TraceSink:
    """A sink that collects nothing, for paths with no run context."""
    return _NULL_SINK
