"""Skill-selection evidence (GCE ticket 10).

Until now the main path registered *every* enabled skill, and the selector
existed only in a self-test.  That wasted tokens, but the worse cost was
epistemic: with everything always loaded there is no way to tell "the skill was
never selected" apart from "the skill was selected and did not help".  Those
demand opposite fixes — better routing versus a better skill — and skill
attribution cannot start until they are distinguishable.

So selection records not just the winners but **why each candidate lost**. The
rejected candidates are the most valuable negative examples available and
nothing in the system captures them today.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Why a candidate did not make the cut.
REASON_NOT_RELEVANT = "not_relevant"
REASON_RANKED_OUT = "ranked_out"  # relevant enough, but outside top-K
REASON_FILTERED = "filtered"  # ownership / permission / disabled
REASON_SELECTOR_UNAVAILABLE = "selector_unavailable"


@dataclass
class SkillCandidate:
    skill_id: str
    score: Optional[float] = None
    selected: bool = False
    reason: str = ""
    rank: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "score": self.score,
            "selected": self.selected,
            "reason": self.reason,
            "rank": self.rank,
        }


@dataclass
class SkillSelection:
    """The full outcome of one selection pass."""

    selected_ids: List[str] = field(default_factory=list)
    candidates: List[SkillCandidate] = field(default_factory=list)
    strategy: str = "llm"
    # True when selection failed and we fell back to registering everything.
    degraded: bool = False
    degrade_reason: str = ""

    def to_event_payload(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "selected": list(self.selected_ids),
            "candidates": [c.to_dict() for c in self.candidates],
            # Explicitly surfaced because "considered but rejected" is the
            # signal skill attribution is built on.
            "rejected": [c.skill_id for c in self.candidates if not c.selected],
        }


def build_selection(
    *,
    all_candidate_ids: List[str],
    selected_ids: List[str],
    strategy: str = "llm",
    scores: Optional[Dict[str, float]] = None,
    degraded: bool = False,
    degrade_reason: str = "",
) -> SkillSelection:
    """Assemble the record, assigning a loss reason to every rejected candidate."""
    scores = scores or {}
    chosen = list(dict.fromkeys(selected_ids))
    chosen_set = set(chosen)

    candidates: List[SkillCandidate] = []
    for index, skill_id in enumerate(chosen, start=1):
        candidates.append(
            SkillCandidate(
                skill_id=skill_id,
                score=scores.get(skill_id),
                selected=True,
                reason="",
                rank=index,
            )
        )
    for skill_id in all_candidate_ids:
        if skill_id in chosen_set:
            continue
        candidates.append(
            SkillCandidate(
                skill_id=skill_id,
                score=scores.get(skill_id),
                selected=False,
                reason=(
                    REASON_SELECTOR_UNAVAILABLE
                    if degraded
                    else (REASON_RANKED_OUT if skill_id in scores else REASON_NOT_RELEVANT)
                ),
            )
        )

    return SkillSelection(
        selected_ids=chosen,
        candidates=candidates,
        strategy=strategy,
        degraded=degraded,
        degrade_reason=degrade_reason,
    )


def select_with_record(
    *,
    user_query: str,
    available_skill_ids: List[str],
    enabled_skill_ids: List[str],
    model: Any = None,
    max_skills: int = 5,
) -> SkillSelection:
    """Run selection and capture the evidence.

    On any failure the caller gets every candidate back — degrading to the old
    "register everything" behaviour. Losing the token saving is annoying;
    failing the user's turn because a *routing optimisation* broke would be
    inexcusable.
    """
    candidate_ids = sorted(set(available_skill_ids) & set(enabled_skill_ids))
    if not candidate_ids:
        return build_selection(all_candidate_ids=[], selected_ids=[], strategy="empty")

    if not user_query or model is None:
        # No query or no model to rank with: keep everything rather than guess.
        return build_selection(
            all_candidate_ids=candidate_ids,
            selected_ids=candidate_ids,
            strategy="passthrough",
            degraded=True,
            degrade_reason="no_query_or_model",
        )

    try:
        from core.agent_skills.selector import select_skills_for_query

        selected = select_skills_for_query(
            user_query=user_query,
            available_skill_ids=available_skill_ids,
            enabled_skill_ids=enabled_skill_ids,
            model=model,
            max_skills=max_skills,
        )
        return build_selection(
            all_candidate_ids=candidate_ids,
            selected_ids=[s for s in (selected or []) if s in set(candidate_ids)],
            strategy="llm",
        )
    except Exception as exc:
        logger.warning("[skill-select] selection failed, registering all: %s", exc)
        return build_selection(
            all_candidate_ids=candidate_ids,
            selected_ids=candidate_ids,
            strategy="fallback",
            degraded=True,
            degrade_reason=str(exc)[:200],
        )
