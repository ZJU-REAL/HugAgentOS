"""Per-user evolution participation (GCE ticket 27 extension).

A switch literally labelled "evolve or not" would misrepresent what a single
user can control.  Memory evolution genuinely is per-user — it is their own
memory.  But skill and workflow candidates come from patterns aggregated across
*many* users' episodes, and ontology is organisation-wide; one person switching
off cannot stop the system distilling a skill from everyone else's traces.

What a user can honestly control, and what the code can actually enforce, is
whether **their own conversations contribute evidence** to the loop.  So that is
what this setting is, and what the label says.

Enforcement lives at one point rather than being scattered: the Episode's
``privacy_class``.  Opting out marks episodes ``private``, and pattern mining
filters those out — so the user still gets their own in-chat card and their own
memory, while nothing of theirs feeds cross-user learning. Anything weaker would
make the promise unverifiable.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.db.engine import SessionLocal

logger = logging.getLogger(__name__)

SETTING_KEY = "evolution_contribution_enabled"

# Episodes from an opted-out user. Recorded for that user's own benefit (their
# card, their history) but excluded from anything cross-user.
PRIVACY_PRIVATE = "private"
PRIVACY_TENANT = "tenant"


def is_contribution_enabled(metadata: Optional[Dict[str, Any]]) -> bool:
    """Whether this user's episodes may feed the evolution loop.

    Defaults to on, matching the existing memory switches — but note that
    "on" here only ever means *within the tenant*: cross-tenant sharing is a
    separate, independently-gated decision (see ``federation``).
    """
    if not metadata:
        return True
    return bool(metadata.get(SETTING_KEY, True))


def privacy_class_for(metadata: Optional[Dict[str, Any]]) -> str:
    """The Episode privacy class implied by the user's setting."""
    return PRIVACY_TENANT if is_contribution_enabled(metadata) else PRIVACY_PRIVATE


def resolve_for_user(user_id: str) -> Dict[str, Any]:
    """Read the setting for one user. Degrades to the default, never raises."""
    if not user_id:
        return {SETTING_KEY: True, "privacy_class": PRIVACY_TENANT}
    try:
        from core.db.models import UserShadow

        with SessionLocal() as db:
            row = db.query(UserShadow).filter(UserShadow.user_id == user_id).first()
            metadata = (getattr(row, "extra_data", None) or {}) if row else {}
    except Exception as exc:
        logger.debug("[evo-settings] read failed for %s: %s", user_id, exc)
        metadata = {}

    enabled = is_contribution_enabled(metadata)
    return {SETTING_KEY: enabled, "privacy_class": privacy_class_for(metadata)}


def update_for_user(user_id: str, enabled: bool) -> Dict[str, Any]:
    """Persist the setting.

    Turning it off does **not** retroactively delete already-contributed
    evidence — that would silently rewrite history other candidates were derived
    from. It stops future episodes from contributing; withdrawing past evidence
    is a separate, explicit action so its consequences stay visible.
    """
    from core.db.models import UserShadow

    with SessionLocal() as db:
        row = db.query(UserShadow).filter(UserShadow.user_id == user_id).first()
        if row is None:
            raise ValueError(f"user not found: {user_id}")
        metadata = dict(getattr(row, "extra_data", None) or {})
        metadata[SETTING_KEY] = bool(enabled)
        row.extra_data = metadata
        db.commit()

    return {
        SETTING_KEY: bool(enabled),
        "privacy_class": PRIVACY_TENANT if enabled else PRIVACY_PRIVATE,
        "note": "仅影响此后新产生的证据；已贡献的证据需单独撤回",
    }


# A candidate is the user's to approve when their own episodes account for at
# least this share of its evidence. Below it the capability was learned mostly
# from other people's work, and one person approving it for everyone is a
# different decision that belongs with an administrator.
OWN_EVIDENCE_THRESHOLD = 0.6


def pending_for_user(user_id: str, *, limit: int = 20) -> List[Dict[str, Any]]:
    """Candidates this user may approve for themselves.

    Scoped deliberately. A skill distilled from someone's own conversations is
    reasonably theirs to accept, and requiring an administrator for it makes the
    loop unusable in practice. But a candidate built mostly from other people's
    evidence activates a capability for the fleet, and letting any single user
    push that is a governance hole wearing the costume of convenience — those
    stay with an administrator.
    """
    if not user_id:
        return []
    try:
        from core.db.models.evolution import EvolutionCandidate, EvolutionEpisode

        with SessionLocal() as db:
            own_ids = {
                row.episode_id
                for row in db.query(EvolutionEpisode.episode_id)
                .filter(EvolutionEpisode.user_id == user_id)
                .all()
            }
            if not own_ids:
                return []

            out: List[Dict[str, Any]] = []
            for candidate in (
                db.query(EvolutionCandidate)
                .filter(EvolutionCandidate.status.in_(("draft", "replay_passed")))
                .order_by(EvolutionCandidate.created_at.desc())
                .limit(200)
                .all()
            ):
                refs = set(candidate.evidence_refs or [])
                if not refs:
                    continue
                mine = refs & own_ids
                share = len(mine) / len(refs)
                if share < OWN_EVIDENCE_THRESHOLD:
                    continue
                out.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "target_kind": candidate.target_kind,
                        "summary": (candidate.hypothesis or "")[:140],
                        "status": candidate.status,
                        "risk_tier": candidate.risk_tier,
                        "your_episodes": len(mine),
                        "total_evidence": len(refs),
                        "own_share": round(share, 3),
                        "tool_sequence": _tool_sequence_of_ir(candidate.ir),
                        "created_at": candidate.created_at.isoformat()
                        if candidate.created_at
                        else None,
                    }
                )
                if len(out) >= limit:
                    break
            return out
    except Exception as exc:
        logger.warning("[evo-settings] pending candidates failed: %s", exc)
        return []


def _tool_sequence_of_ir(ir: Optional[Dict[str, Any]]) -> List[str]:
    for change in (ir or {}).get("changes") or []:
        sequence = change.get("tool_sequence")
        if isinstance(sequence, list) and sequence:
            return [str(t) for t in sequence]
    return []


def approve_for_user(candidate_id: str, user_id: str) -> Dict[str, Any]:
    """Approve a candidate and activate it **for this user only**.

    The resulting skill is private to them, so accepting it cannot change what
    anyone else's agent does. That is what makes personal approval safe enough
    to sit in a settings panel rather than behind an administrator.
    """
    from core.evolution.activation import ActivationError, materialise_skill_candidate

    allowed = {c["candidate_id"] for c in pending_for_user(user_id, limit=200)}
    if candidate_id not in allowed:
        raise PermissionError(
            "该候选主要来自他人的对话证据，需管理员审批后才能全局启用"
        )

    result = materialise_skill_candidate(
        candidate_id,
        approver=f"user:{user_id}",
        force=True,
        owner_user_id=user_id,
    )
    result["scope"] = "personal"
    return result


def summarise_contributions(user_id: str) -> Dict[str, Any]:
    """What this user's conversations have actually produced.

    Answers the question the settings panel raises: "I left it on — so what did
    it do?" Shows the chain from the user's own episodes through to candidates,
    with each candidate's true governance state rather than an optimistic one.
    """
    empty = {
        "episodes": 0,
        "contributed_episodes": 0,
        "private_episodes": 0,
        "candidates": [],
        "memory_written": 0,
    }
    if not user_id:
        return empty

    try:
        from core.db.models.evolution import (
            EvolutionCandidate,
            EvolutionEpisode,
            EvolutionTraceEvent,
        )

        with SessionLocal() as db:
            episodes = (
                db.query(EvolutionEpisode)
                .filter(EvolutionEpisode.user_id == user_id)
                .order_by(EvolutionEpisode.created_at.desc())
                .limit(500)
                .all()
            )
            if not episodes:
                return empty

            episode_ids = {e.episode_id for e in episodes}
            contributed = [e for e in episodes if e.privacy_class != PRIVACY_PRIVATE]

            memory_written = 0
            for event in (
                db.query(EvolutionTraceEvent)
                .filter(EvolutionTraceEvent.episode_id.in_(list(episode_ids)))
                .all()
            ):
                if event.event_type == "memory.retrieved":
                    memory_written += int((event.payload or {}).get("injected") or 0)

            # Candidates whose evidence includes any of this user's episodes.
            candidates = []
            for candidate in (
                db.query(EvolutionCandidate)
                .order_by(EvolutionCandidate.created_at.desc())
                .limit(200)
                .all()
            ):
                refs = set(candidate.evidence_refs or [])
                shared = refs & episode_ids
                if not shared:
                    continue
                candidates.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "target_kind": candidate.target_kind,
                        "summary": (candidate.hypothesis or "")[:120],
                        # The real state — a draft says draft.
                        "status": candidate.status,
                        "risk_tier": candidate.risk_tier,
                        "your_episodes": len(shared),
                        "total_evidence": len(refs),
                        "created_at": candidate.created_at.isoformat()
                        if candidate.created_at
                        else None,
                    }
                )

            return {
                "episodes": len(episodes),
                "contributed_episodes": len(contributed),
                "private_episodes": len(episodes) - len(contributed),
                "candidates": candidates,
                "memory_written": memory_written,
            }
    except Exception as exc:
        logger.warning("[evo-settings] contribution summary failed: %s", exc)
        return empty
