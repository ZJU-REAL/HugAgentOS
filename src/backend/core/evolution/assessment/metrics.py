"""Cycle metrics (GCE ticket 26).

Four rolling series: skill reuse, memory coverage, orchestration efficiency and
human-intervention rate.  One rule dominates the design — **no back-filling.**

Before this shipped there were no cycle metrics, so there is no honest way to
show a trend for that period.  An invented curve is worse than a missing one
precisely because it is believed: everyone downstream reasons from a shape that
never happened, and the error is nearly impossible to spot later.  So a period
without data reports "collecting data" and draws nothing.

The same computation feeds the console and the in-chat card. Two implementations
would drift, and the first symptom would be a user seeing one number in the
transcript and an administrator seeing another for the same period.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CYCLE_DAYS = 7
MIN_EPISODES_PER_CYCLE = 5


def _cycle_bounds(index: int, *, now: Optional[datetime] = None):
    end = (now or datetime.now(timezone.utc)) - timedelta(days=CYCLE_DAYS * index)
    return end - timedelta(days=CYCLE_DAYS), end


def compute_cycle(db, *, index: int, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Metrics for one cycle, or ``None`` when there is too little data.

    Returning ``None`` rather than zeros is deliberate: zero is a measurement,
    "we do not know" is not, and rendering the latter as the former turns a data
    gap into a visible decline that never happened.
    """
    from core.db.models.evolution import EvolutionCandidate, EvolutionEpisode, EvolutionTraceEvent

    start, end = _cycle_bounds(index, now=now)
    episodes = (
        db.query(EvolutionEpisode)
        .filter(
            EvolutionEpisode.created_at >= start,
            EvolutionEpisode.created_at < end,
        )
        .all()
    )
    if len(episodes) < MIN_EPISODES_PER_CYCLE:
        return None

    episode_ids = [e.episode_id for e in episodes]
    events = (
        db.query(EvolutionTraceEvent)
        .filter(EvolutionTraceEvent.episode_id.in_(episode_ids))
        .all()
    )

    skill_selected = 0
    skill_considered = 0
    memory_hits = 0
    for event in events:
        payload = event.payload or {}
        if event.event_type == "skill.selected":
            selected = payload.get("selected") or []
            rejected = payload.get("rejected") or []
            skill_selected += len(selected)
            skill_considered += len(selected) + len(rejected)
        elif event.event_type == "memory.retrieved":
            memory_hits += int(payload.get("injected") or 0)

    graded = [e for e in episodes if e.quality_score is not None]
    succeeded = [e for e in graded if (e.quality_score or 0) >= 0.65]

    candidates = (
        db.query(EvolutionCandidate)
        .filter(
            EvolutionCandidate.created_at >= start,
            EvolutionCandidate.created_at < end,
        )
        .all()
    )
    manual = [c for c in candidates if c.approved_by]

    return {
        "cycle": index,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "episodes": len(episodes),
        "skill_reuse_rate": round(skill_selected / skill_considered, 4)
        if skill_considered
        else None,
        "memory_coverage": round(memory_hits / len(episodes), 4) if episodes else None,
        "orchestration_efficiency": round(len(succeeded) / len(graded), 4)
        if graded
        else None,
        "human_intervention_rate": round(len(manual) / len(candidates), 4)
        if candidates
        else None,
    }


def recent_cycles(db, *, limit: int = 12, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Most recent cycles that actually have data, oldest first.

    Cycles without enough data are omitted rather than emitted as zeros — the
    client renders "collecting data" for the gap.
    """
    out: List[Dict[str, Any]] = []
    for index in range(limit):
        try:
            cycle = compute_cycle(db, index=index, now=now)
        except Exception as exc:
            logger.debug("[metrics] cycle %d failed: %s", index, exc)
            continue
        if cycle is not None:
            out.append(cycle)
    return list(reversed(out))


def current_cycle_delta(db, *, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The "cycle N → N+1" line under the in-chat card.

    Returns ``None`` unless two consecutive cycles both have data; a single
    cycle cannot show a change, and inventing a previous value to compare
    against would fabricate a trend.
    """
    current = compute_cycle(db, index=0, now=now)
    previous = compute_cycle(db, index=1, now=now)
    if current is None or previous is None:
        return None
    return {
        "from": 1,
        "to": 0,
        "skill_reuse_rate": {
            "before": previous.get("skill_reuse_rate"),
            "after": current.get("skill_reuse_rate"),
        },
        "human_intervention_rate": {
            "before": previous.get("human_intervention_rate"),
            "after": current.get("human_intervention_rate"),
        },
    }
