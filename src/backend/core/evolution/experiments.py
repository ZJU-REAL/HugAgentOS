"""Experiment programme (GCE tickets 41 / 42 / 43 / 44 / 45).

To claim that joint evolution beats independent per-engine updating, six system
configurations must be runnable over the *same* code, the *same* tasks and the
*same* base model.  Anything else and the comparison is confounded before it
starts.

**B4 (naive joint) is the decisive control.**  It represents "put the three
engines in one product and let each learn from every failure" — precisely the
approach this design argues is integration rather than a learning mechanism.  If
the credit-assigned configuration cannot beat B4, the main contribution does not
hold, and the honest response is to stop and re-examine the mechanism rather
than to keep running the remaining experiments.

**Update budgets must be equalised.**  Otherwise a configuration that simply
changes more things looks like one that learns better.

Ablations are runtime switches, not code branches — six divergent copies would
drift apart and the comparison would quietly stop meaning anything.  The
ontology-invariant ablation is reachable only in an isolated experiment
environment; a switch that disables safety checks must not exist in a
configuration that production can reach.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ── System matrix (ticket 41) ────────────────────────────────────────────────

B0_STATIC = "B0_static"
B1_MEMORY_ONLY = "B1_memory_only"
B2_SKILL_ONLY = "B2_skill_only"
B3_WORKFLOW_ONLY = "B3_workflow_only"
B4_NAIVE_JOINT = "B4_naive_joint"
OURS_GCE = "ours_gce"

ALL_SYSTEMS = (
    B0_STATIC,
    B1_MEMORY_ONLY,
    B2_SKILL_ONLY,
    B3_WORKFLOW_ONLY,
    B4_NAIVE_JOINT,
    OURS_GCE,
)


@dataclass(frozen=True)
class SystemConfig:
    """One arm of the comparison, expressed purely as switches."""

    name: str
    memory: bool = False
    skill: bool = False
    workflow: bool = False
    credit_assignment: bool = False
    governed_release: bool = False
    multi_timescale: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "memory": self.memory,
            "skill": self.skill,
            "workflow": self.workflow,
            "credit_assignment": self.credit_assignment,
            "governed_release": self.governed_release,
            "multi_timescale": self.multi_timescale,
        }


SYSTEM_MATRIX: Dict[str, SystemConfig] = {
    B0_STATIC: SystemConfig(B0_STATIC),
    B1_MEMORY_ONLY: SystemConfig(B1_MEMORY_ONLY, memory=True, governed_release=True),
    B2_SKILL_ONLY: SystemConfig(B2_SKILL_ONLY, skill=True, governed_release=True),
    B3_WORKFLOW_ONLY: SystemConfig(B3_WORKFLOW_ONLY, workflow=True, governed_release=True),
    # Every engine on, no credit assignment: each learns from every failure.
    B4_NAIVE_JOINT: SystemConfig(
        B4_NAIVE_JOINT, memory=True, skill=True, workflow=True, governed_release=True
    ),
    OURS_GCE: SystemConfig(
        OURS_GCE,
        memory=True,
        skill=True,
        workflow=True,
        credit_assignment=True,
        governed_release=True,
        multi_timescale=True,
    ),
}


@dataclass
class ExperimentRun:
    system: str
    task_set_version: str
    base_model: str
    update_budget: int
    updates_applied: int = 0
    code_version: str = ""
    dataset_snapshot: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "system": self.system,
            "task_set_version": self.task_set_version,
            "base_model": self.base_model,
            "update_budget": self.update_budget,
            "updates_applied": self.updates_applied,
            "code_version": self.code_version,
        }


def validate_comparable(runs: Sequence[ExperimentRun]) -> Tuple[bool, List[str]]:
    """Whether a set of runs may legitimately be compared."""
    problems: List[str] = []
    if len(runs) < 2:
        return True, problems

    for field_name in ("task_set_version", "base_model", "update_budget"):
        values = {getattr(r, field_name) for r in runs}
        if len(values) > 1:
            problems.append(f"{field_name} differs across arms: {sorted(values)}")

    # Budget parity is about what was *actually spent*, not merely allowed:
    # an arm that applied twice as many updates looks better for the wrong
    # reason.
    applied = [r.updates_applied for r in runs]
    if applied and max(applied) - min(applied) > max(1, int(0.1 * max(applied))):
        problems.append(f"update counts not equalised: {applied}")

    return (not problems), problems


# ── Ablations (ticket 42) ────────────────────────────────────────────────────

ABL_NO_COUNTERFACTUAL = "no_counterfactual"
ABL_NO_NO_UPDATE = "no_no_update"
ABL_NO_ONTOLOGY_INVARIANT = "no_ontology_invariant"
ABL_NO_SHADOW = "no_shadow"
ABL_RULE_VS_LEARNED = "rule_vs_learned_credit"

ALL_ABLATIONS = (
    ABL_NO_COUNTERFACTUAL,
    ABL_NO_NO_UPDATE,
    ABL_NO_ONTOLOGY_INVARIANT,
    ABL_NO_SHADOW,
    ABL_RULE_VS_LEARNED,
)

# Switches that must never be reachable outside an isolated experiment
# environment. Disabling a safety invariant is a research manoeuvre, not a
# configuration option.
PRODUCTION_FORBIDDEN_ABLATIONS = frozenset({ABL_NO_ONTOLOGY_INVARIANT})


def ablation_allowed(name: str, *, environment: str) -> Tuple[bool, str]:
    if name not in ALL_ABLATIONS:
        return False, f"unknown ablation {name}"
    if name in PRODUCTION_FORBIDDEN_ABLATIONS and environment != "experiment":
        return False, f"{name} is not reachable outside an isolated experiment"
    return True, ""


# ── Longitudinal metrics (ticket 43) ─────────────────────────────────────────


@dataclass
class LongitudinalMetrics:
    success_rate: List[float] = field(default_factory=list)
    forward_transfer: Optional[float] = None
    catastrophic_forgetting: Optional[float] = None
    negative_transfer_rate: Optional[float] = None
    evolution_half_life: Optional[float] = None
    version_drift: Optional[float] = None
    attribution_top1: Optional[float] = None
    no_update_specificity: Optional[float] = None
    update_regret: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            k: (round(v, 4) if isinstance(v, float) else v)
            for k, v in self.__dict__.items()
        }


def forward_transfer(*, unseen_after: float, unseen_baseline: float) -> float:
    """Did what was learned help on task types never trained on?"""
    return round(unseen_after - unseen_baseline, 4)


def catastrophic_forgetting(
    *, before: Dict[str, float], after: Dict[str, float]
) -> float:
    """Largest drop across previously-reliable task types."""
    drops = [
        before[k] - after[k]
        for k in before
        if k in after and before[k] - after[k] > 0
    ]
    return round(max(drops), 4) if drops else 0.0


def evolution_half_life(activation_lifetimes_days: Sequence[float]) -> Optional[float]:
    """Median time an activated change survives before replacement or retirement.

    Too short means the system is thrashing; too long means it has stopped
    learning. Reporting it keeps both failure modes visible.
    """
    values = sorted(v for v in activation_lifetimes_days if v is not None)
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return round(values[middle], 4)
    return round((values[middle - 1] + values[middle]) / 2, 4)


def update_regret(*, activated: int, later_rolled_back: int) -> Optional[float]:
    """Share of activations later undone."""
    if activated <= 0:
        return None
    return round(later_rolled_back / activated, 4)


def wilson_interval(successes: int, total: int, z: float = 1.96) -> Tuple[float, float]:
    """Confidence interval for a proportion.

    Reported alongside every rate: a point estimate on a small sample invites
    exactly the over-reading this programme is trying to avoid.
    """
    if total <= 0:
        return (0.0, 0.0)
    p = successes / total
    denominator = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denominator
    margin = (
        z * ((p * (1 - p) / total + z**2 / (4 * total**2)) ** 0.5)
    ) / denominator
    return (round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4))


# ── Non-stationary testbed (ticket 44) ───────────────────────────────────────

PERTURB_TOOL_DESCRIPTION = "tool_description_changed"
PERTURB_API_SCHEMA = "api_schema_changed"
PERTURB_SKILL_AVAILABILITY = "skill_availability_changed"
PERTURB_DOMAIN_CONSTRAINT = "domain_constraint_changed"

ALL_PERTURBATIONS = (
    PERTURB_TOOL_DESCRIPTION,
    PERTURB_API_SCHEMA,
    PERTURB_SKILL_AVAILABILITY,
    PERTURB_DOMAIN_CONSTRAINT,
)


@dataclass
class NonStationaryResult:
    perturbation: str
    detection_cycles: Optional[int] = None
    recovery_cycles: Optional[int] = None
    forgetting_cycles: Optional[int] = None
    attributed_to: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "perturbation": self.perturbation,
            "detection_cycles": self.detection_cycles,
            "recovery_cycles": self.recovery_cycles,
            "forgetting_cycles": self.forgetting_cycles,
            "attributed_to": self.attributed_to,
        }


def expected_attribution(perturbation: str) -> str:
    """What a correct assigner should say about each perturbation.

    The schema-change case is the direct empirical test of the pseudo-evolution
    defence: it must land in the environment bucket and produce no asset
    candidate at all.
    """
    return {
        PERTURB_API_SCHEMA: "environment",
        PERTURB_TOOL_DESCRIPTION: "environment",
        PERTURB_SKILL_AVAILABILITY: "skill",
        PERTURB_DOMAIN_CONSTRAINT: "ontology",
    }.get(perturbation, "no_update")


# ── Task sets (ticket 45) ────────────────────────────────────────────────────

SPLIT_TRAIN = "train_replay"
SPLIT_HOLDOUT = "holdout"
SPLIT_GOLD = "gold"
ALL_SPLITS = (SPLIT_TRAIN, SPLIT_HOLDOUT, SPLIT_GOLD)


@dataclass
class TaskSet:
    version: str
    splits: Dict[str, List[str]] = field(default_factory=dict)
    deterministic_scoring: bool = True

    def isolation_violations(self) -> List[str]:
        """Task ids appearing in more than one split.

        Leakage between splits is the failure that makes every downstream number
        meaningless while looking perfectly healthy.
        """
        problems: List[str] = []
        names = list(self.splits)
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                overlap = set(self.splits[a]) & set(self.splits[b])
                if overlap:
                    problems.append(f"{a}∩{b}: {sorted(overlap)[:5]}")
        return problems

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "sizes": {k: len(v) for k, v in self.splits.items()},
            "deterministic_scoring": self.deterministic_scoring,
        }


def required_sample_size(*, baseline_rate: float, mde: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """Paired-design sample size for a target minimum detectable effect.

    Exists so the benchmark size is derived from the effect worth detecting,
    rather than a round number chosen first and hoped to be adequate.
    """
    z_alpha, z_beta = 1.96, 0.84
    p = max(0.01, min(0.99, baseline_rate))
    variance = p * (1 - p)
    n = ((z_alpha + z_beta) ** 2) * variance / max(1e-6, mde**2)
    return int(n) + 1


def scoring_is_acceptable(*, uses_llm_judge: bool, split: str) -> Tuple[bool, str]:
    """Public-benchmark scoring must be deterministic.

    Claiming a benchmark result that a model graded itself on is not a
    comparable result.
    """
    if split == SPLIT_GOLD and uses_llm_judge:
        return False, "gold split requires deterministic scoring"
    return True, ""
