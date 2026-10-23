"""Release control — the only component allowed to move a version pointer.

(GCE tickets 17 / 18 / 19.)

Self-evolution without staged rollout and rollback is just automatic config
editing.  This module is what makes the difference: a candidate travels
``draft → replay_passed → shadow → canary → active → retired`` and can drop into
``rejected`` or ``rolled_back`` from anywhere along the way.

Design commitments worth stating outright:

* **Only this module writes an active pointer.**  Generators propose; nothing
  else promotes.
* **A generator cannot approve its own proposal.**  Enforced by comparing the
  proposer against the approver, not merely documented.
* **Shadow output never reaches a user.**  It exists to gather comparison data
  at zero user-visible risk.
* **Canary bucketing is stable.**  The same user stays in the same bucket for a
  release — a user flip-flopping between versions is both a bad experience and
  a broken experiment.
* **The rollback target is recorded at promotion time.**  Knowing where to
  return to is only useful if you wrote it down before things went wrong.

Canary is also the one place in this whole design that produces a genuinely
randomised comparison. Replay is a screening filter — its sample comes from a
past policy and its tool environment has since drifted. So the headline effect
size should come from here, and replay should only report how well it screens.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Stages ───────────────────────────────────────────────────────────────────

DRAFT = "draft"
REPLAY_PASSED = "replay_passed"
SHADOW = "shadow"
CANARY = "canary"
ACTIVE = "active"
RETIRED = "retired"
REJECTED = "rejected"
ROLLED_BACK = "rolled_back"

# Legal transitions. Anything absent is refused — an illegal jump is how a
# candidate would reach production without the verification its tier demands.
TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    DRAFT: (REPLAY_PASSED, REJECTED),
    REPLAY_PASSED: (SHADOW, CANARY, REJECTED),
    SHADOW: (CANARY, REJECTED, ROLLED_BACK),
    CANARY: (ACTIVE, REJECTED, ROLLED_BACK),
    ACTIVE: (RETIRED, ROLLED_BACK),
    RETIRED: (),
    REJECTED: (),
    ROLLED_BACK: (),
}

TERMINAL_STAGES = (RETIRED, REJECTED, ROLLED_BACK)
IN_FLIGHT_STAGES = (DRAFT, REPLAY_PASSED, SHADOW, CANARY)

# Ramp steps. Small first step on purpose: the cost of a bad release scales with
# how much traffic saw it before anyone noticed.
CANARY_STEPS = (5, 25, 100)

ROLLBACK_GUARDRAIL = "guardrail"
ROLLBACK_MANUAL = "manual"


class ReleaseError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def is_legal_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, ())


def assert_transition(current: str, target: str) -> None:
    if not is_legal_transition(current, target):
        raise ReleaseError(
            "illegal_transition", f"不允许的状态跳转 {current} → {target}"
        )


def next_traffic(current_percent: int) -> int:
    """The next ramp step, or the current value once fully ramped."""
    for step in CANARY_STEPS:
        if step > current_percent:
            return step
    return CANARY_STEPS[-1]


# ── Canary bucketing ─────────────────────────────────────────────────────────


def in_canary_bucket(
    *, release_id: str, subject_id: str, traffic_percent: int
) -> bool:
    """Stable hash bucketing.

    Keyed on (release, subject) so a subject's assignment is fixed for the
    life of a release but independent across releases — otherwise the same
    unlucky cohort would receive every experiment.

    Deterministic, so the assignment can be recomputed during analysis instead
    of being stored per request.
    """
    if traffic_percent <= 0:
        return False
    if traffic_percent >= 100:
        return True
    if not subject_id:
        return False
    digest = hashlib.sha256(f"{release_id}:{subject_id}".encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) % 100) < traffic_percent


# ── Guardrails ───────────────────────────────────────────────────────────────


@dataclass
class GuardrailBreach:
    metric: str
    observed: float
    threshold: float
    window: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric": self.metric,
            "observed": self.observed,
            "threshold": self.threshold,
            "window": self.window,
        }


# Direction each metric must not exceed. Encoded rather than inferred because
# getting a sign wrong here would auto-roll-back every healthy release — or
# worse, never roll back a broken one.
_HIGHER_IS_WORSE = {
    "failure_rate",
    "denial_rate",
    "p95_latency_ms",
    "cost_usd",
    "safety_violations",
}
_LOWER_IS_WORSE = {"success_rate", "quality_score"}


def evaluate_guardrails(
    observed: Dict[str, float], thresholds: Dict[str, float], *, window: str = ""
) -> List[GuardrailBreach]:
    """Return every breach; an empty list means the release may continue."""
    breaches: List[GuardrailBreach] = []
    for metric, limit in (thresholds or {}).items():
        if metric not in observed:
            continue
        value = observed[metric]
        if metric in _HIGHER_IS_WORSE and value > limit:
            breaches.append(GuardrailBreach(metric, value, limit, window))
        elif metric in _LOWER_IS_WORSE and value < limit:
            breaches.append(GuardrailBreach(metric, value, limit, window))
    return breaches


def should_auto_rollback(breaches: List[GuardrailBreach]) -> bool:
    """Any breach rolls back.

    No "two out of three" softening: each guardrail is already the level at
    which the release is deemed unacceptable, so requiring several to trip
    would just move the real threshold somewhere nobody wrote down.
    """
    return bool(breaches)


# ── Verification requirements ────────────────────────────────────────────────


def missing_verification(
    risk_tier: str, completed: List[str], target_stage: str
) -> List[str]:
    """What a candidate still owes before entering ``target_stage``."""
    from core.evolution.ir import required_verification

    required = list(required_verification(risk_tier))
    needed_by_stage = {
        SHADOW: ["replay"],
        CANARY: ["replay", "shadow"],
        ACTIVE: required,
    }.get(target_stage, [])
    return [item for item in needed_by_stage if item in required and item not in completed]


def can_promote(
    *,
    current_stage: str,
    target_stage: str,
    risk_tier: str,
    completed_verification: List[str],
    proposer: str,
    approver: Optional[str],
) -> Tuple[bool, str]:
    """Whether a promotion is allowed, and why not if it is not."""
    if not is_legal_transition(current_stage, target_stage):
        return False, f"illegal_transition:{current_stage}->{target_stage}"

    # The generator must not be the approver. This is the structural version of
    # "a learner cannot be its own judge" — asserted in code, not in a doc.
    if target_stage == ACTIVE:
        if not approver:
            return False, "approval_required"
        if approver == proposer:
            return False, "self_approval_forbidden"

    outstanding = missing_verification(risk_tier, completed_verification, target_stage)
    if outstanding:
        return False, f"missing_verification:{','.join(outstanding)}"

    return True, ""


def new_release_id() -> str:
    return f"rel_{uuid.uuid4().hex[:20]}"


def now() -> datetime:
    return datetime.now(timezone.utc)
