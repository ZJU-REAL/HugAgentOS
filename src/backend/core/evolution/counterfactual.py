"""Counterfactual attribution and the decision objective (GCE tickets 28 / 30 / 31).

**Ticket 28 — attribution by single-variable substitution.**  Rules narrow the
suspects; this confirms responsibility by re-running the same episode with one
asset swapped or removed while everything else is frozen.  Note this asks a
different question from the replay engine: replay asks "is this *candidate*
better than the base?" (the candidate already exists); this asks "would the
outcome have differed without this asset?" (no candidate exists yet).  They
share the executor and cassette but not the logic.

Removal and substitution are genuinely different counterfactuals — "what if this
memory had never been injected" is not "what if it had said something else" —
and attribution needs both.

**Ticket 30 — the decision objective.**  Responsibility alone does not say
whether a change is worth making.  Without cost, risk and complexity terms, a
tiny improvement requiring surgery on core orchestration ranks equal to a
zero-risk confidence tweak, and the system spends its whole replay budget on
changes humans will reject.

**Ticket 31 — calibration.**  Replay screens, canary decides. How well replay
screens is an empirical question, and the answer is what sets the threshold.
Without it, "we screen with replay and confirm with canary" is a slogan.

The honest boundary, recorded with every verdict: this is engineering
attribution under controlled replay, not a randomised causal experiment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

MODE_SUBSTITUTE = "substitute"
MODE_REMOVE = "remove"

HONEST_BOUNDARY = (
    "受控回放下的工程归因，非随机化因果证明；高风险结论仍需人审与在线证据"
)


@dataclass
class CounterfactualProbe:
    """One "what if" applied to a single asset."""

    asset_kind: str
    asset_id: str
    mode: str = MODE_REMOVE
    replacement_version: str = ""

    def label(self) -> str:
        if self.mode == MODE_REMOVE:
            return f"remove:{self.asset_kind}:{self.asset_id}"
        return f"substitute:{self.asset_kind}:{self.asset_id}->{self.replacement_version}"


@dataclass
class ProbeOutcome:
    probe: CounterfactualProbe
    baseline_success: bool
    probed_success: bool

    @property
    def changed(self) -> bool:
        return self.baseline_success != self.probed_success

    @property
    def improved(self) -> bool:
        return (not self.baseline_success) and self.probed_success


@dataclass
class AttributionResult:
    credits: Dict[str, float] = field(default_factory=dict)
    probes_run: int = 0
    verdict: str = "no_update"
    boundary_note: str = HONEST_BOUNDARY
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "credits": {k: round(v, 3) for k, v in self.credits.items()},
            "probes_run": self.probes_run,
            "verdict": self.verdict,
            "boundary_note": self.boundary_note,
            "notes": self.notes,
        }


def run_counterfactual_attribution(
    probes: Sequence[CounterfactualProbe],
    run_probe: Callable[[Optional[CounterfactualProbe]], bool],
    *,
    repeats: int = 3,
) -> AttributionResult:
    """Isolate responsibility by probing one asset at a time.

    ``repeats`` guards against sampling noise being read as a causal effect —
    a single differing run is not evidence, and this is the cheapest available
    defence against that mistake.
    """
    result = AttributionResult()
    if not probes:
        result.notes.append("no probes supplied")
        return result

    baseline_runs = [run_probe(None) for _ in range(repeats)]
    baseline_success = sum(baseline_runs) > len(baseline_runs) / 2
    result.probes_run += repeats

    for probe in probes:
        runs = [run_probe(probe) for _ in range(repeats)]
        result.probes_run += repeats
        probed_success = sum(runs) > len(runs) / 2
        outcome = ProbeOutcome(probe, baseline_success, probed_success)

        if outcome.improved:
            # Removing or replacing it made a failing task pass ⇒ it was
            # implicated in the failure.
            result.credits[probe.asset_kind] = max(
                result.credits.get(probe.asset_kind, 0.0), 0.8
            )
        elif outcome.changed:
            # It mattered, but in the other direction: this asset was helping.
            result.credits[probe.asset_kind] = min(
                result.credits.get(probe.asset_kind, 0.0), -0.5
            )
        else:
            result.credits.setdefault(probe.asset_kind, 0.0)

    positive = {k: v for k, v in result.credits.items() if v > 0}
    if not positive:
        # Nothing changed the outcome ⇒ the cause lies outside the assets we can
        # change (model, data, environment). Saying so is the useful answer.
        result.verdict = "no_update"
        result.notes.append("no substitution changed the outcome; cause likely non-asset")
    elif len(positive) == 1:
        result.verdict = next(iter(positive))
    else:
        result.verdict = "unidentifiable"
        result.notes.append(f"multiple assets implicated: {sorted(positive)}")
    return result


# ── Decision objective (ticket 30) ───────────────────────────────────────────


@dataclass
class DecisionTerms:
    utility_gain: float = 0.0
    cost: float = 0.0
    risk: float = 0.0
    complexity: float = 0.0

    def score(self, *, lam: float = 1.0, mu: float = 1.0, nu: float = 0.5) -> float:
        return self.utility_gain - lam * self.cost - mu * self.risk - nu * self.complexity

    def to_dict(self) -> Dict[str, Any]:
        return {
            "utility_gain": round(self.utility_gain, 4),
            "cost": round(self.cost, 4),
            "risk": round(self.risk, 4),
            "complexity": round(self.complexity, 4),
        }


def measured_cost(*, replay_tokens: int, latency_delta_ms: int, token_budget: int) -> float:
    """Cost from measurements, not a constant.

    A hard-coded weight would drift away from reality the moment model pricing
    or the replay set size changed, and nobody would notice.
    """
    token_term = replay_tokens / max(1, token_budget)
    latency_term = max(0.0, latency_delta_ms) / 5000.0
    return round(min(1.0, token_term + latency_term), 4)


def measured_risk(*, risk_tier: str, historical_rollback_rate: float) -> float:
    """Risk from the tier plus how often similar candidates were rolled back."""
    tier_weight = {"low": 0.1, "medium": 0.35, "high": 0.7}.get(risk_tier, 0.7)
    return round(min(1.0, tier_weight + historical_rollback_rate * 0.5), 4)


def measured_complexity(*, dependent_assets: int) -> float:
    """Complexity from dependency breadth — how much else this change touches."""
    return round(min(1.0, dependent_assets / 10.0), 4)


def rank_candidates(
    candidates: Sequence[Dict[str, Any]], *, min_score: Optional[float] = None
) -> List[Dict[str, Any]]:
    """Order candidates by net value, optionally dropping ones not worth evaluating.

    Ranking and filtering are separate decisions, so filtering is opt-in: a
    batch where every candidate scores negative is still meaningfully ordered,
    and silently returning nothing would hide that from the caller. Passing
    ``min_score`` is what stops the replay budget going to high-risk, low-gain
    changes a reviewer would reject anyway.
    """
    scored = []
    for candidate in candidates:
        terms = candidate.get("terms")
        if not isinstance(terms, DecisionTerms):
            continue
        score = terms.score()
        scored.append({**candidate, "score": round(score, 4), "terms": terms.to_dict()})
    scored.sort(key=lambda c: c["score"], reverse=True)
    if min_score is None:
        return scored
    return [c for c in scored if c["score"] >= min_score]


# ── Replay/canary calibration (ticket 31) ────────────────────────────────────


@dataclass
class CalibrationReport:
    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    true_negative: int = 0
    per_kind: Dict[str, Dict[str, int]] = field(default_factory=dict)
    sufficient: bool = False

    @property
    def precision(self) -> float:
        denominator = self.true_positive + self.false_positive
        return self.true_positive / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        denominator = self.true_positive + self.false_negative
        return self.true_positive / denominator if denominator else 0.0

    def recommendation(self) -> str:
        if not self.sufficient:
            return "insufficient_samples"
        # Precision is undefined when the screen passed nothing; reading the
        # 0/0 case as "poor precision" would recommend tightening a filter that
        # is already rejecting everything — exactly backwards.
        made_positive_calls = (self.true_positive + self.false_positive) > 0
        if made_positive_calls and self.precision < 0.5:
            return "tighten_replay_threshold"
        if self.recall < 0.7:
            # Over-strict screening quietly discards real improvements, which is
            # invisible in the metrics unless it is named.
            return "loosen_replay_threshold"
        return "calibrated"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "confusion": {
                "tp": self.true_positive,
                "fp": self.false_positive,
                "fn": self.false_negative,
                "tn": self.true_negative,
            },
            "per_kind": self.per_kind,
            "sufficient": self.sufficient,
            "recommendation": self.recommendation(),
            "note": "主效应量来自灰度；回放只报告其作为筛选器的准确率与召回率",
        }


def calibrate_replay_against_canary(
    paired: Sequence[Dict[str, Any]], *, min_samples: int = 20
) -> CalibrationReport:
    """Compare replay's screening verdict with the canary's randomised outcome.

    ``paired`` entries: ``{"kind": ..., "replay_improved": bool, "canary_improved": bool}``.
    """
    report = CalibrationReport()
    for item in paired:
        replay_yes = bool(item.get("replay_improved"))
        canary_yes = bool(item.get("canary_improved"))
        kind = str(item.get("kind") or "unknown")
        bucket = report.per_kind.setdefault(kind, {"tp": 0, "fp": 0, "fn": 0, "tn": 0})

        if replay_yes and canary_yes:
            report.true_positive += 1
            bucket["tp"] += 1
        elif replay_yes and not canary_yes:
            report.false_positive += 1
            bucket["fp"] += 1
        elif not replay_yes and canary_yes:
            report.false_negative += 1
            bucket["fn"] += 1
        else:
            report.true_negative += 1
            bucket["tn"] += 1

    report.sufficient = len(paired) >= min_samples
    return report
