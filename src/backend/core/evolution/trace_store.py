"""Persistence for trace events (GCE ticket 04).

Deliberately thin.  The only interesting decision here is what happens when a
write fails: it is logged and swallowed.  Evidence collection is valuable but it
sits alongside the user's answer, and an observability plane that can take down
chat is worse than no observability plane at all.

Duplicate ``(message_id, seq)`` pairs are ignored rather than raising — a retried
flush must be a no-op, not an error.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Optional, Sequence

from core.db.engine import SessionLocal
from core.db.models.evolution import EvolutionTraceEvent
from core.evolution.events import TraceEvent

logger = logging.getLogger(__name__)


def persist_events(events: Sequence[TraceEvent], *, episode_id: str = "") -> int:
    """Write buffered events; returns how many rows were actually inserted."""
    if not events:
        return 0

    written = 0
    try:
        with SessionLocal() as db:
            existing = {
                (row.message_id, row.seq)
                for row in db.query(
                    EvolutionTraceEvent.message_id, EvolutionTraceEvent.seq
                ).filter(
                    EvolutionTraceEvent.message_id.in_(
                        {e.message_id for e in events if e.message_id}
                    )
                )
            }
            for event in events:
                if (event.message_id, event.seq) in existing:
                    continue
                db.add(
                    EvolutionTraceEvent(
                        event_id=event.event_id,
                        episode_id=episode_id or None,
                        message_id=event.message_id,
                        run_id=event.run_id,
                        chat_id=event.chat_id,
                        user_id=event.user_id,
                        seq=event.seq,
                        event_type=event.event_type,
                        actor_type=event.actor_type,
                        actor_id=event.actor_id,
                        asset_kind=event.asset_kind,
                        asset_version_id=event.asset_version_id,
                        payload=event.payload,
                        payload_ref=event.payload_ref,
                        created_at=event.created_at,
                    )
                )
                written += 1
            db.commit()
    except Exception as exc:
        logger.warning("[trace-store] persist failed (%d events): %s", len(events), exc)
        return 0
    return written


def load_events(message_id: str) -> List[EvolutionTraceEvent]:
    """All events for a run, in emission order."""
    if not message_id:
        return []
    try:
        with SessionLocal() as db:
            return (
                db.query(EvolutionTraceEvent)
                .filter(EvolutionTraceEvent.message_id == message_id)
                .order_by(EvolutionTraceEvent.seq.asc())
                .all()
            )
    except Exception as exc:
        logger.warning("[trace-store] load failed for %s: %s", message_id, exc)
        return []


def attach_episode(message_id: str, episode_id: str) -> int:
    """Back-fill the episode id onto events written before assembly ran."""
    if not message_id or not episode_id:
        return 0
    try:
        with SessionLocal() as db:
            updated = (
                db.query(EvolutionTraceEvent)
                .filter(
                    EvolutionTraceEvent.message_id == message_id,
                    EvolutionTraceEvent.episode_id.is_(None),
                )
                .update({EvolutionTraceEvent.episode_id: episode_id})
            )
            db.commit()
            return int(updated or 0)
    except Exception as exc:
        logger.warning("[trace-store] attach episode failed: %s", exc)
        return 0
