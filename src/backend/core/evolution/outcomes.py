"""Multi-signal outcome judgement (GCE ticket 09).

Today the only usable signal for "did this turn go well?" is a thumbs up.
Driving evolution from that alone rewards flattery; driving it from task success
alone rewards over-reaching tool calls.  So the outcome is assembled from
several independent signals across four dimensions — quality, cost, risk,
latency — and one rule is enforced above all:

**No single judge can decide evolution on its own.**

Concretely: a thumbs-up cannot make an outcome successful if the gate denied the
output, and a high LLM score cannot outvote a deterministic check that the
delivered file will not open.  Signals that are cheap to forge (ratings) are
capped in influence; signals that are expensive to forge (a triaged bug report,
a deterministic verification) carry more.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Signal identifiers
SIG_FEEDBACK = "explicit_feedback"
SIG_PLAN_ACCEPT = "plan_acceptance"
SIG_REVIEWER = "reviewer_verdict"
SIG_ARTIFACT = "artifact_openable"
SIG_TOOL_ERRORS = "tool_errors"
SIG_ONTOLOGY = "ontology_risk"
SIG_COST = "cost"
SIG_LATENCY = "latency"
SIG_USER_REPORT = "user_report"

# How much each signal may move the quality score. Forgeable signals are
# deliberately capped below the deterministic ones: a batch of fake thumbs-ups
# must never be able to carry a turn on its own.
_WEIGHTS = {
    SIG_FEEDBACK: 0.20,
    SIG_PLAN_ACCEPT: 0.25,
    SIG_REVIEWER: 0.25,
    SIG_ARTIFACT: 0.30,
    SIG_TOOL_ERRORS: 0.25,
    SIG_USER_REPORT: 0.40,
}

# Signals nothing may override. A denied gate means the turn failed, regardless
# of how happy anyone was with the text.
_HARD_FAIL_SIGNALS = {SIG_ONTOLOGY}


@dataclass
class Signal:
    """One observation contributing to the outcome."""

    name: str
    # Normalised to [-1, 1]; negative is evidence of failure.
    value: float
    confidence: float = 1.0
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_forgeable(self) -> bool:
        """Whether a motivated user could fabricate this signal cheaply."""
        return self.name == SIG_FEEDBACK


@dataclass
class Outcome:
    """The assembled verdict for one turn."""

    quality_score: Optional[float] = None
    cost_usd: Optional[float] = None
    latency_ms: Optional[int] = None
    risk_result: str = "unknown"
    signals: List[Signal] = field(default_factory=list)
    # Drops when signals are missing. Attribution must be able to distinguish
    # "we know this went badly" from "we barely know anything about this run".
    confidence: float = 0.0
    hard_failed: bool = False
    verdict: str = "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "quality_score": self.quality_score,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "risk_result": self.risk_result,
            "confidence": round(self.confidence, 3),
            "hard_failed": self.hard_failed,
            "verdict": self.verdict,
            "signals": [
                {
                    "name": s.name,
                    "value": round(s.value, 3),
                    "confidence": round(s.confidence, 3),
                    "detail": s.detail,
                }
                for s in self.signals
            ],
        }


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def build_outcome(
    *,
    feedback_rating: Optional[str] = None,
    plan_accepted: Optional[bool] = None,
    reviewer_verdict: Optional[str] = None,
    artifacts_openable: Optional[bool] = None,
    tool_error_count: int = 0,
    tool_call_count: int = 0,
    ontology_denied: bool = False,
    cost_usd: Optional[float] = None,
    latency_ms: Optional[int] = None,
    user_report: Optional[Dict[str, Any]] = None,
) -> Outcome:
    """Fold the available signals into one outcome.

    Every argument is optional: a turn with almost no signals yields a low
    confidence rather than a confident guess.
    """
    signals: List[Signal] = []

    if feedback_rating in ("like", "dislike"):
        signals.append(
            Signal(
                name=SIG_FEEDBACK,
                value=1.0 if feedback_rating == "like" else -1.0,
                # Capped confidence: a rating is cheap to produce and easy to
                # game, so it informs but never decides.
                confidence=0.5,
                detail={"rating": feedback_rating},
            )
        )

    if plan_accepted is not None:
        signals.append(
            Signal(name=SIG_PLAN_ACCEPT, value=1.0 if plan_accepted else -1.0)
        )

    if reviewer_verdict:
        value = {"pass": 1.0, "revise": -0.3, "escalate": -0.7}.get(reviewer_verdict, 0.0)
        signals.append(
            Signal(name=SIG_REVIEWER, value=value, detail={"verdict": reviewer_verdict})
        )

    if artifacts_openable is not None:
        # Deterministic: the file either opens or it does not. No model opinion
        # can outvote this.
        signals.append(
            Signal(name=SIG_ARTIFACT, value=1.0 if artifacts_openable else -1.0)
        )

    if tool_call_count > 0:
        error_rate = tool_error_count / max(1, tool_call_count)
        signals.append(
            Signal(
                name=SIG_TOOL_ERRORS,
                value=_clamp(1.0 - 2.0 * error_rate),
                detail={"errors": tool_error_count, "calls": tool_call_count},
            )
        )

    if user_report:
        # A triaged bug report is the closest thing to ground truth available:
        # a human looked at it and reached a disposition.
        resolved = str(user_report.get("disposition") or "").lower()
        value = -1.0 if resolved in ("confirmed", "bug", "accepted") else 0.2
        signals.append(
            Signal(name=SIG_USER_REPORT, value=value, detail=dict(user_report))
        )

    if ontology_denied:
        signals.append(
            Signal(name=SIG_ONTOLOGY, value=-1.0, detail={"denied": True})
        )

    hard_failed = any(s.name in _HARD_FAIL_SIGNALS and s.value < 0 for s in signals)

    scored = [s for s in signals if s.name in _WEIGHTS]
    if scored:
        total_weight = sum(_WEIGHTS[s.name] * s.confidence for s in scored)
        weighted = sum(_WEIGHTS[s.name] * s.confidence * s.value for s in scored)
        raw = weighted / total_weight if total_weight else 0.0
        # Map [-1, 1] onto [0, 1] so downstream consumers see a conventional score.
        quality = _clamp((raw + 1.0) / 2.0, 0.0, 1.0)
    else:
        quality = None

    # Confidence reflects breadth of evidence, not strength of opinion. Forgeable
    # signals contribute less because they say less about what really happened.
    evidence = sum(0.5 if s.is_forgeable else 1.0 for s in signals)
    confidence = _clamp(evidence / 4.0, 0.0, 1.0)

    if hard_failed:
        verdict = "failed"
        # A denied gate is a failure no matter what anyone rated it.
        quality = 0.0
    elif quality is None:
        verdict = "unknown"
    elif quality >= 0.65:
        verdict = "success"
    elif quality <= 0.35:
        verdict = "failed"
    else:
        verdict = "mixed"

    return Outcome(
        quality_score=quality,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        risk_result="denied" if ontology_denied else "clear",
        signals=signals,
        confidence=confidence,
        hard_failed=hard_failed,
        verdict=verdict,
    )


def merge_late_signal(existing: Dict[str, Any], **late) -> Dict[str, Any]:
    """Fold a signal that arrived after settlement back into a stored outcome.

    Feedback often lands hours later and a filed report can take days. Ignoring
    those would systematically bias the evidence toward turns nobody revisited.
    The recomputation is recorded so an outcome that changed is visible as such.
    """
    prior = {
        s["name"]: s for s in (existing or {}).get("signals", []) if isinstance(s, dict)
    }
    kwargs: Dict[str, Any] = {}
    if SIG_FEEDBACK in prior:
        kwargs["feedback_rating"] = prior[SIG_FEEDBACK].get("detail", {}).get("rating")
    if SIG_ONTOLOGY in prior:
        kwargs["ontology_denied"] = True
    kwargs.update({k: v for k, v in late.items() if v is not None})

    refreshed = build_outcome(**kwargs).to_dict()
    refreshed["revised"] = True
    refreshed["previous_verdict"] = (existing or {}).get("verdict")
    return refreshed
