"""Flatten episodes and their trace events into the views the engines read."""
from __future__ import annotations
from typing import Any, Dict, List
from core.db.engine import SessionLocal
from core.db.models.evolution import EvolutionEpisode, EvolutionTraceEvent
from core.evolution.profiles.user_settings_constants import PRIVACY_PRIVATE
import logging
logger = logging.getLogger(__name__)

def view_for(episode, events) -> Dict[str, Any]:
    """One Episode plus its events, flattened into what the engines read.

    Three fields here did not exist and each blocked an engine outright:

    ``memory_refs`` now carries the **stable content ref**, not the raw content
    hash. The two look alike and are not: the ref is what the overlay keys on,
    so an op proposed against a hash could never be applied to anything.

    ``skills_opened`` is what the model chose to read, as opposed to
    ``skill_sequence``, which is what it was offered. Every decrement decision
    depends on the difference.

    ``selection_degraded`` marks turns where ranking could not run and
    everything loaded. Counting those as selections is what made every skill's
    offer count identical, and therefore useless.
    """
    from core.evolution.evidence.events import EV_MEMORY_RETRIEVED, EV_SKILL_OPENED, EV_SKILL_SELECTED, EV_TOOL_CALLED
    from core.memory.ref_shadow import build_ref_id
    from core.memory.retrieval_types import LAYER_FACT, LAYER_GRAPH

    tools: List[str] = []
    skills: List[str] = []
    memory_refs: List[str] = []
    graph_refs: List[str] = []
    graph_relations: List[Dict[str, Any]] = []
    skills_opened: List[Dict[str, Any]] = []
    selection_degraded = False
    error_count = 0

    user_id = episode.user_id or ""
    for event in events:
        payload = event.payload or {}
        if event.event_type == EV_TOOL_CALLED:
            name = payload.get("tool_name")
            if name:
                tools.append(str(name))
            if str(payload.get("status") or "") == "error":
                error_count += 1
        elif event.event_type == EV_SKILL_SELECTED:
            if payload.get("degraded"):
                selection_degraded = True
            skills.extend(str(s) for s in (payload.get("selected") or []))
        elif event.event_type == EV_SKILL_OPENED:
            skills_opened.append(
                {
                    "skill_id": str(payload.get("skill_id") or ""),
                    "tools_after_open": [str(t) for t in (payload.get("tools_after_open") or [])],
                }
            )
        elif event.event_type == EV_MEMORY_RETRIEVED:
            for item in payload.get("items") or []:
                content_hash = str(item.get("content_hash") or "")
                if not content_hash:
                    continue
                memory_refs.append(
                    build_ref_id(
                        layer=str(item.get("layer") or LAYER_FACT),
                        user_id=user_id,
                        workspace_id=str(item.get("workspace_id") or "default"),
                        content_hash=content_hash,
                    )
                )
            # L3 is deliberately tracked separately from L2. A graph relation
            # is declarative context, not a procedure, and must never enter the
            # memory→skill compiler as though it were an instruction.
            for relation in payload.get("graph") or []:
                content_hash = str(relation.get("content_hash") or "")
                if not content_hash:
                    continue
                graph_refs.append(
                    build_ref_id(
                        layer=LAYER_GRAPH,
                        user_id=user_id,
                        workspace_id=str(relation.get("workspace_id") or "default"),
                        content_hash=content_hash,
                    )
                )
                # The full reference, kept for the evidence resolver: a pack's
                # L3 entry must carry the store's stable relation_id so an
                # activated skill's graph evidence can be resolved back to the
                # exact Neo4j edge — a bare ref id cannot do that.
                graph_relations.append(
                    {
                        "relation_id": str(relation.get("relation_id") or ""),
                        "content_hash": content_hash,
                        "predicate": str(relation.get("predicate") or ""),
                        "confidence": relation.get("confidence") or 0.0,
                        "workspace_id": str(relation.get("workspace_id") or "default"),
                    }
                )

    outcome = episode.outcome or {}
    return {
        "episode_id": episode.episode_id,
        "chat_id": episode.chat_id or "",
        "user_id": user_id,
        "tenant_id": episode.tenant_id or "default",
        "verdict": outcome.get("verdict")
        or ("success" if (episode.quality_score or 0) >= 0.65 else "unknown"),
        "tool_sequence": tools,
        "skill_sequence": skills,
        "skills_opened": skills_opened,
        "selection_degraded": selection_degraded,
        "memory_refs": memory_refs,
        "graph_refs": graph_refs,
        "graph_relations": graph_relations,
        "task_type": episode.task_type or "chat",
        "backfilled": bool(episode.backfilled),
        # Repeated tool errors are the observable form of "this run was stuck",
        # which sub-agent derivation needs and nothing else recorded.
        "stalled": error_count >= 3,
        # What the user actually asked. Clustering only ever saw tool sequences
        # before this, so "the same question keeps coming back" was not a
        # detectable event. Sanitised at write time, so this is the preview
        # rather than the raw text.
        "objective": episode.objective_preview or "",
        "objective_hash": episode.objective_hash or "",
        "attributed_to": str(outcome.get("attributed_to") or ""),
        # Needed to place an episode before or after an activation. Without it
        # negative-transfer detection has no way to split the corpus and returns
        # nothing on every corpus — wired, running, and structurally incapable
        # of finding anything.
        "created_at": episode.created_at,
    }


def load_episode_views(*, limit: int = 500, since_days: int = 30) -> List[Dict[str, Any]]:
    """Flatten Episodes plus their trace events into the shape pattern mining wants.

    Backfilled Episodes are included on purpose: they cannot be replayed, but
    they are perfectly good for discovering what recurs — which is exactly why
    the backfill was worth doing.
    """
    views: List[Dict[str, Any]] = []
    try:
        with SessionLocal() as db:
            # The single enforcement point for per-user opt-out. Episodes from
            # users who declined to contribute are still recorded (they power
            # that user's own card and history) but never feed cross-user
            # pattern mining. Filtering here rather than at each call site is
            # what makes the promise checkable.
            episodes = (
                db.query(EvolutionEpisode)
                .filter(EvolutionEpisode.privacy_class != PRIVACY_PRIVATE)
                .order_by(EvolutionEpisode.created_at.desc())
                .limit(limit)
                .all()
            )
            if not episodes:
                return []

            ids = [e.episode_id for e in episodes]
            events = (
                db.query(EvolutionTraceEvent)
                .filter(EvolutionTraceEvent.episode_id.in_(ids))
                .order_by(EvolutionTraceEvent.seq.asc())
                .all()
            )
            by_episode: Dict[str, List[EvolutionTraceEvent]] = {}
            for event in events:
                by_episode.setdefault(event.episode_id, []).append(event)

            for episode in episodes:
                views.append(view_for(episode, by_episode.get(episode.episode_id, [])))
    except Exception as exc:
        logger.warning("[evolution-loop] loading episodes failed: %s", exc)
        return []
    return views
