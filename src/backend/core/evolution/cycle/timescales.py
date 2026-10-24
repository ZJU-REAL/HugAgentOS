"""Multi-timescale governance and stability monitoring (GCE tickets 37 / 38).

Different assets must evolve at different speeds: memory in seconds-to-minutes,
skills in hours-to-days, workflows in batches-to-weeks, ontology on a human
cycle.  This is not a nicety — if everything drifts quickly at once the system
becomes non-stationary and no metric movement can be attributed to anything.
It is what keeps "joint evolution" from degenerating into "joint jitter".

Ticket 38 adds the safety net that single-release guardrails structurally
cannot provide.  A guardrail asks "did *this* change break something?"  But a
hundred individually harmless changes can still leave the system worse, and no
single guardrail ever fires.  Only a frozen gold-standard set, re-run on a
schedule, can see that.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

LAYER_MEMORY = "memory"
LAYER_SKILL = "skill"
LAYER_WORKFLOW = "workflow"
LAYER_ONTOLOGY = "ontology"

# Ordered fast → slow. A change in a fast layer must never cascade into a slow
# one; cross-layer amplification is how a small memory tweak ends up rewriting
# the domain model.
LAYER_ORDER = (LAYER_MEMORY, LAYER_SKILL, LAYER_WORKFLOW, LAYER_ONTOLOGY)

DEFAULT_COOLDOWN_S: Dict[str, int] = {
    LAYER_MEMORY: 60,
    LAYER_SKILL: 6 * 3600,
    LAYER_WORKFLOW: 3 * 24 * 3600,
    # Ontology has no automatic cadence at all: it moves when a human moves it.
    LAYER_ONTOLOGY: 14 * 24 * 3600,
}

# Cap on simultaneous activations across all layers in one window. Beyond this,
# attribution for any of them becomes hopeless.
MAX_CONCURRENT_ACTIVATIONS = 3


@dataclass
class RateLimiter:
    """Enforces per-layer cooldowns and cross-layer arbitration."""

    cooldowns: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_COOLDOWN_S))
    last_activation: Dict[str, datetime] = field(default_factory=dict)
    in_flight: Dict[str, bool] = field(default_factory=dict)
    frozen: bool = False
    tenant_overrides: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def cooldown_for(self, layer: str, tenant: str = "default") -> int:
        override = self.tenant_overrides.get(tenant, {})
        return override.get(layer, self.cooldowns.get(layer, 3600))

    def may_activate(
        self,
        layer: str,
        *,
        now: Optional[datetime] = None,
        tenant: str = "default",
        concurrent_activations: int = 0,
    ) -> Tuple[bool, str]:
        """Whether ``layer`` may activate a change right now."""
        if self.frozen:
            return False, "globally_frozen"

        moment = now or datetime.now(timezone.utc)
        last = self.last_activation.get(layer)
        cooldown = self.cooldown_for(layer, tenant)
        if last is not None and (moment - last).total_seconds() < cooldown:
            remaining = int(cooldown - (moment - last).total_seconds())
            return False, f"cooldown:{remaining}s"

        # A fast layer must not drag a slow one along behind it.
        index = LAYER_ORDER.index(layer) if layer in LAYER_ORDER else len(LAYER_ORDER)
        for slower in LAYER_ORDER[index + 1 :]:
            if self.in_flight.get(slower):
                return False, f"slower_layer_in_flight:{slower}"

        if concurrent_activations >= MAX_CONCURRENT_ACTIVATIONS:
            return False, "too_many_concurrent_activations"

        return True, ""

    def record_activation(self, layer: str, *, now: Optional[datetime] = None) -> None:
        self.last_activation[layer] = now or datetime.now(timezone.utc)

    def freeze(self) -> None:
        """Stop every layer at once — the incident-time bleed stop."""
        self.frozen = True

    def unfreeze(self) -> None:
        self.frozen = False


# ── Stability monitoring (ticket 38) ─────────────────────────────────────────


@dataclass
class GoldStandardResult:
    task_id: str
    passed: bool


@dataclass
class DriftReport:
    current_score: float = 0.0
    baseline_score: float = 0.0
    forgotten_task_types: List[str] = field(default_factory=list)
    version_churn: float = 0.0
    recent_activations: List[str] = field(default_factory=list)
    should_freeze: bool = False
    alerts: List[str] = field(default_factory=list)

    @property
    def delta(self) -> float:
        return self.current_score - self.baseline_score

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current_score": round(self.current_score, 4),
            "baseline_score": round(self.baseline_score, 4),
            "delta": round(self.delta, 4),
            "forgotten_task_types": self.forgotten_task_types,
            "version_churn": round(self.version_churn, 4),
            "recent_activations": self.recent_activations,
            "should_freeze": self.should_freeze,
            "alerts": self.alerts,
        }


# How far the gold-standard score may slip before activations are frozen.
GOLD_DROP_THRESHOLD = 0.05
FORGETTING_THRESHOLD = 0.10
CHURN_THRESHOLD = 0.5


def evaluate_drift(
    *,
    gold_results: Sequence[GoldStandardResult],
    baseline_score: float,
    task_type_before: Dict[str, float],
    task_type_after: Dict[str, float],
    activations_in_window: Sequence[str],
    window_capacity: int = 10,
) -> DriftReport:
    """Look for whole-system degradation that no single guardrail can see."""
    report = DriftReport(baseline_score=baseline_score)
    report.recent_activations = list(activations_in_window)

    if gold_results:
        report.current_score = sum(r.passed for r in gold_results) / len(gold_results)

    if report.delta <= -GOLD_DROP_THRESHOLD:
        report.should_freeze = True
        report.alerts.append(
            f"gold_standard_regression:{report.delta:.3f}"
        )

    for task_type, before in (task_type_before or {}).items():
        after = (task_type_after or {}).get(task_type)
        if after is None:
            continue
        if before - after >= FORGETTING_THRESHOLD:
            # A capability that used to work reliably no longer does — the
            # direct signature of catastrophic forgetting.
            report.forgotten_task_types.append(task_type)

    if report.forgotten_task_types:
        report.alerts.append(f"forgetting:{report.forgotten_task_types}")

    report.version_churn = len(activations_in_window) / max(1, window_capacity)
    if report.version_churn >= CHURN_THRESHOLD:
        report.alerts.append(f"version_churn:{report.version_churn:.2f}")

    return report


def bulk_rollback_plan(activations: Sequence[str], target_bundle_id: str) -> Dict[str, Any]:
    """Return to a known-good asset combination.

    Per-asset rollback cannot repair degradation caused by the *combination* of
    several individually-fine changes, which is exactly the case drift detection
    surfaces.
    """
    return {
        "target_bundle_id": target_bundle_id,
        "reverting": list(activations),
        "reason": "gold_standard_regression",
        "kind": "bulk",
    }


def gold_set_is_protected(*, actor_is_privileged: bool, audited: bool) -> Tuple[bool, str]:
    """Guard the gold set itself.

    If the benchmark can be edited freely, the cheapest way to fix a failing
    score is to change the questions — and the metric silently stops meaning
    anything.
    """
    if not actor_is_privileged:
        return False, "gold_set_requires_privileged_actor"
    if not audited:
        return False, "gold_set_change_must_be_audited"
    return True, ""
