"""Evaluator committee and monotone safety (GCE tickets 39 / 40).

**Ticket 39.**  If the proposer is also the sole judge, the system optimises for
satisfying the judge rather than doing the work — reward hacking with extra
steps.  So generator, evaluator, approver and runtime gate are four separate
roles, enforced in code.  Machine-checkable properties (does the file open, is
the structure valid, is the constraint satisfied) go to deterministic verifiers:
they cannot be argued with, which is the whole point.

Disagreement escalates rather than averaging. Averaging conflates "two reviewers
strongly disagree" with "both were lukewarm", and those call for opposite
responses.

**Ticket 40 — monotone safety.**  The subtlest failure of a self-improving
system is not a bad candidate; it is the system learning to remove the checks
that block it.  Any loop optimising a pass rate finds that relaxing the standard
is cheaper than meeting it, and the metrics look *better* while it happens.

So the safety-check set may only grow. Removal is unreachable from automation;
it needs a privileged human act with an immutable audit record. Loosening a
threshold counts as removal — otherwise "tighten to meaninglessness" is an open
back door.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

ROLE_GENERATOR = "generator"
ROLE_EVALUATOR = "evaluator"
ROLE_APPROVER = "approver"
ROLE_RUNTIME_GATE = "runtime_gate"

LENS_QUALITY = "quality"
LENS_COST = "cost"
LENS_RISK = "risk"
LENS_GENERALISATION = "generalisation"
ALL_LENSES = (LENS_QUALITY, LENS_COST, LENS_RISK, LENS_GENERALISATION)

VERDICT_APPROVE = "approve"
VERDICT_REJECT = "reject"
VERDICT_ABSTAIN = "abstain"

# Spread above which the committee escalates instead of aggregating.
DISAGREEMENT_THRESHOLD = 0.5


@dataclass
class Judgement:
    lens: str
    verdict: str
    score: float
    judge_id: str
    model: str = ""
    reason: str = ""
    deterministic: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lens": self.lens,
            "verdict": self.verdict,
            "score": round(self.score, 3),
            "judge_id": self.judge_id,
            "model": self.model,
            "reason": self.reason,
            "deterministic": self.deterministic,
        }


@dataclass
class CommitteeResult:
    verdict: str = VERDICT_ABSTAIN
    judgements: List[Judgement] = field(default_factory=list)
    escalated: bool = False
    escalation_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "escalated": self.escalated,
            "escalation_reason": self.escalation_reason,
            "judgements": [j.to_dict() for j in self.judgements],
        }


class SelfJudgementError(Exception):
    """Raised when a proposer tries to judge its own candidate."""


def assert_not_self_judging(*, proposer: str, judge_id: str) -> None:
    """The structural form of "a learner cannot be its own judge"."""
    if proposer and judge_id and proposer == judge_id:
        raise SelfJudgementError(
            f"generator {proposer} cannot evaluate its own candidate"
        )


def convene(
    judgements: Sequence[Judgement],
    *,
    proposer: str = "",
    risk_tier: str = "medium",
    generator_model: str = "",
) -> CommitteeResult:
    """Aggregate independent judgements, escalating on genuine disagreement."""
    result = CommitteeResult(judgements=list(judgements))

    for judgement in judgements:
        assert_not_self_judging(proposer=proposer, judge_id=judgement.judge_id)

    if not judgements:
        result.escalated = True
        result.escalation_reason = "no_judgements"
        return result

    # High-risk candidates must not be judged by the model that produced them:
    # a shared model shares blind spots, so agreement proves little.
    if risk_tier == "high" and generator_model:
        model_judges = [
            j for j in judgements if j.model == generator_model and not j.deterministic
        ]
        if len(model_judges) == len(judgements):
            result.escalated = True
            result.escalation_reason = "high_risk_requires_independent_verifier"
            return result

    scores = [j.score for j in judgements]
    spread = max(scores) - min(scores)
    if spread >= DISAGREEMENT_THRESHOLD:
        # Averaging would conflate "strongly opposed" with "both lukewarm".
        result.escalated = True
        result.escalation_reason = f"disagreement_spread:{spread:.2f}"
        return result

    # A deterministic rejection is final: it is a fact, not an opinion.
    for judgement in judgements:
        if judgement.deterministic and judgement.verdict == VERDICT_REJECT:
            result.verdict = VERDICT_REJECT
            return result

    approvals = sum(1 for j in judgements if j.verdict == VERDICT_APPROVE)
    rejections = sum(1 for j in judgements if j.verdict == VERDICT_REJECT)
    if rejections > 0:
        result.verdict = VERDICT_REJECT
    elif approvals >= len(judgements) - 1:
        result.verdict = VERDICT_APPROVE
    else:
        result.verdict = VERDICT_ABSTAIN
    return result


def evaluator_failure_verdict() -> CommitteeResult:
    """Fail closed.

    An evaluator that timed out has not approved anything; treating silence as
    consent is how an outage becomes an unreviewed release.
    """
    return CommitteeResult(
        verdict=VERDICT_REJECT,
        escalated=True,
        escalation_reason="evaluator_unavailable_fail_closed",
    )


# ── Monotone safety (ticket 40) ──────────────────────────────────────────────


class MonotonicityViolation(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class SafetyCheckSet:
    """The set of active safety checks, plus their thresholds."""

    checks: Set[str] = field(default_factory=set)
    thresholds: Dict[str, float] = field(default_factory=dict)
    # Direction each threshold must move to be *stricter*. Encoded because
    # "stricter" is not inferable from the number alone.
    stricter_is_higher: Set[str] = field(default_factory=set)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "checks": sorted(self.checks),
            "thresholds": dict(self.thresholds),
        }


def apply_automated_change(
    current: SafetyCheckSet,
    *,
    add_checks: Sequence[str] = (),
    remove_checks: Sequence[str] = (),
    new_thresholds: Optional[Dict[str, float]] = None,
) -> SafetyCheckSet:
    """Apply a change proposed by automation.

    Additions are permitted. Removals and relaxations are not reachable from
    here at all — not "discouraged", unreachable.
    """
    if remove_checks:
        raise MonotonicityViolation(
            "removal_not_reachable_by_automation",
            f"自动流程不得删除安全检查: {sorted(remove_checks)}",
        )

    for name, value in (new_thresholds or {}).items():
        if name not in current.thresholds:
            continue
        previous = current.thresholds[name]
        stricter_higher = name in current.stricter_is_higher
        relaxed = value < previous if stricter_higher else value > previous
        if relaxed:
            # Relaxing to the point of meaninglessness is removal wearing a
            # different hat; it takes the same human path.
            raise MonotonicityViolation(
                "threshold_relaxation_is_removal",
                f"放宽阈值等同删除检查，须走人工路径: {name} {previous} → {value}",
            )

    updated = SafetyCheckSet(
        checks=set(current.checks) | set(add_checks),
        thresholds={**current.thresholds, **(new_thresholds or {})},
        stricter_is_higher=set(current.stricter_is_higher),
    )
    return updated


def apply_human_removal(
    current: SafetyCheckSet,
    *,
    remove_checks: Sequence[str],
    actor: str,
    reason: str,
    audited: bool,
) -> Tuple[SafetyCheckSet, Dict[str, Any]]:
    """The only path that removes a check. Requires a named actor and an audit."""
    if not actor:
        raise MonotonicityViolation("actor_required", "删除安全检查必须署名")
    if not reason:
        raise MonotonicityViolation("reason_required", "删除安全检查必须填写理由")
    if not audited:
        raise MonotonicityViolation("audit_required", "删除安全检查必须写入不可变审计")

    updated = SafetyCheckSet(
        checks=set(current.checks) - set(remove_checks),
        thresholds=dict(current.thresholds),
        stricter_is_higher=set(current.stricter_is_higher),
    )
    record = {
        "action": "remove_safety_checks",
        "removed": sorted(remove_checks),
        "actor": actor,
        "reason": reason,
        "alert": True,
    }
    return updated, record


def verify_monotonicity(
    history: Sequence[SafetyCheckSet],
) -> Tuple[bool, List[str]]:
    """Periodic self-check: is the current set a superset of every earlier one?

    A one-off design guarantee decays; this is what turns it into a property
    that keeps being true.
    """
    problems: List[str] = []
    if not history:
        return True, problems
    latest = history[-1]
    for index, earlier in enumerate(history[:-1]):
        missing = earlier.checks - latest.checks
        if missing:
            problems.append(f"checks lost since snapshot {index}: {sorted(missing)}")
    return (not problems), problems
