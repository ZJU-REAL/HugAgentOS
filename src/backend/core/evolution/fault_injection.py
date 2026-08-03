"""Fault-injection attribution benchmark (GCE ticket 12).

The one part of this whole design that cannot verify itself is *whether the
attribution is any good* — real failures come with no ground-truth label.  So we
manufacture the labels: take tasks that are known to pass, break exactly one
asset, and check whether the assigner points at the thing we broke.

This is the highest-value experiment in the programme and among the cheapest: no
production traffic, no long online window.  It is also the only way to turn "we
can tell which component to fix" from an assertion into a finding.

**It is the Phase 1 exit gate.**  If Top-1 accuracy and No-Update specificity
are not good enough under controlled injection, attribution is not usable, and
nothing downstream — candidate generation, promotion chains, release control —
should be built on top of it. Fix the evaluation first.

**Honest limitation of the current stage.**  The cases below synthesise the
*feature vector* directly rather than running a perturbed task and extracting
features from its trace.  Against the rule-based assigner that makes clean
single-fault cases close to tautological — near-perfect scores here say the
rules are self-consistent, not that attribution works on real traces.  The
numbers become meaningful once features are extracted from genuinely perturbed
Episodes (which needs the executor from ticket 16).  What *is* already load
bearing: the confound and null families, the multi-fault degradation curve, and
the degenerate-assigner checks — those catch real design errors today.

Six injection families, including the two that are easy to forget:
- a **confound bucket** (KB gap / weak model / tool outage), whose correct answer
  is "not a mutable asset";
- a **null control** where nothing is broken at all, whose correct answer is
  No-Update. Without it, a system biased toward "every failure needs *some*
  edit" scores perfectly while being badly wrong.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.evolution.credit import (
    ALL_TARGETS,
    NON_ASSET_TARGETS,
    T_ENVIRONMENT,
    T_MEMORY,
    T_MODEL,
    T_NO_UPDATE,
    T_ONTOLOGY,
    T_RETRIEVAL,
    T_SKILL,
    T_ORCHESTRATION,
    CreditFeatures,
    assign_credit,
)

logger = logging.getLogger(__name__)

# Injection families → the target a correct assigner should name.
FAMILY_MEMORY = "memory"
FAMILY_SKILL = "skill"
FAMILY_WORKFLOW = "workflow"
FAMILY_ONTOLOGY = "ontology"
FAMILY_CONFOUND = "confound"
FAMILY_NULL = "null"

ALL_FAMILIES = (
    FAMILY_MEMORY,
    FAMILY_SKILL,
    FAMILY_WORKFLOW,
    FAMILY_ONTOLOGY,
    FAMILY_CONFOUND,
    FAMILY_NULL,
)


@dataclass
class InjectionCase:
    """One labelled sample: a perturbation plus the answer it should elicit."""

    case_id: str
    family: str
    variant: str
    features: CreditFeatures
    expected_target: str
    # Confound cases are correct as long as the verdict is *some* non-asset
    # target; which one matters less than refusing to patch an asset.
    expect_non_asset: bool = False

    def is_correct(self, selected: str) -> bool:
        if self.expect_non_asset:
            return selected in NON_ASSET_TARGETS
        return selected == self.expected_target


def _memory_cases(n: int) -> List[InjectionCase]:
    """Stale preference / conflicting fact / wrong entity alias."""
    variants = ("stale_preference", "conflicting_fact", "wrong_entity_alias")
    cases = []
    for i in range(n):
        variant = variants[i % len(variants)]
        cases.append(
            InjectionCase(
                case_id=f"mem-{i:03d}",
                family=FAMILY_MEMORY,
                variant=variant,
                features=CreditFeatures(
                    outcome_verdict="failed",
                    outcome_confidence=0.9,
                    memory_injected=2 + (i % 3),
                    memory_conflicts_current_request=True,
                ),
                expected_target=T_MEMORY,
            )
        )
    return cases


def _skill_cases(n: int) -> List[InjectionCase]:
    """Key step deleted / tools narrowed / wrong parameter example."""
    variants = ("step_deleted", "tools_narrowed", "bad_param_example")
    cases = []
    for i in range(n):
        cases.append(
            InjectionCase(
                case_id=f"skl-{i:03d}",
                family=FAMILY_SKILL,
                variant=variants[i % len(variants)],
                features=CreditFeatures(
                    outcome_verdict="failed",
                    outcome_confidence=0.9,
                    repeated_tool_subsequence=True,
                    skill_selected_count=1,
                    skill_rejected_count=2,
                ),
                expected_target=T_SKILL,
            )
        )
    return cases


def _workflow_cases(n: int) -> List[InjectionCase]:
    """Steps reordered (verification moved late) / budget cut / reviewer off."""
    variants = ("verify_moved_late", "budget_cut", "reviewer_disabled")
    cases = []
    for i in range(n):
        cases.append(
            InjectionCase(
                case_id=f"wfl-{i:03d}",
                family=FAMILY_WORKFLOW,
                variant=variants[i % len(variants)],
                features=CreditFeatures(
                    outcome_verdict="failed",
                    outcome_confidence=0.9,
                    loop_stagnation=True,
                    no_diff_iterations=2 + (i % 2),
                    step_order_suspect=True,
                ),
                expected_target=T_ORCHESTRATION,
            )
        )
    return cases


def _ontology_cases(n: int) -> List[InjectionCase]:
    """An over-strict constraint producing false denials."""
    cases = []
    for i in range(n):
        cases.append(
            InjectionCase(
                case_id=f"ont-{i:03d}",
                family=FAMILY_ONTOLOGY,
                variant="over_strict_constraint",
                features=CreditFeatures(
                    outcome_verdict="failed",
                    outcome_confidence=0.9,
                    gate_denied_count=3 + (i % 3),
                    gate_false_positive_evidence=True,
                ),
                expected_target=T_ONTOLOGY,
            )
        )
    return cases


def _confound_cases(n: int) -> List[InjectionCase]:
    """KB document removed / weaker model / tool returning 5xx."""
    variants = (
        ("kb_removed", dict(kb_empty_result=True), T_RETRIEVAL),
        ("weak_model", dict(model_capability_miss=True), T_MODEL),
        ("tool_outage", dict(tool_5xx_count=3), T_ENVIRONMENT),
        ("schema_drift", dict(tool_schema_error=True), T_ENVIRONMENT),
    )
    cases = []
    for i in range(n):
        variant, kwargs, expected = variants[i % len(variants)]
        cases.append(
            InjectionCase(
                case_id=f"cnf-{i:03d}",
                family=FAMILY_CONFOUND,
                variant=variant,
                features=CreditFeatures(
                    outcome_verdict="failed", outcome_confidence=0.9, **kwargs
                ),
                expected_target=expected,
                expect_non_asset=True,
            )
        )
    return cases


def _null_cases(n: int) -> List[InjectionCase]:
    """Nothing broken. The right answer is to change nothing."""
    cases = []
    for i in range(n):
        cases.append(
            InjectionCase(
                case_id=f"nul-{i:03d}",
                family=FAMILY_NULL,
                variant="healthy",
                features=CreditFeatures(
                    outcome_verdict="success",
                    outcome_confidence=0.9,
                    memory_injected=1 if i % 2 else 0,
                    skill_selected_count=1,
                ),
                expected_target=T_NO_UPDATE,
            )
        )
    return cases


_BUILDERS: Dict[str, Callable[[int], List[InjectionCase]]] = {
    FAMILY_MEMORY: _memory_cases,
    FAMILY_SKILL: _skill_cases,
    FAMILY_WORKFLOW: _workflow_cases,
    FAMILY_ONTOLOGY: _ontology_cases,
    FAMILY_CONFOUND: _confound_cases,
    FAMILY_NULL: _null_cases,
}


def build_benchmark(per_family: int = 30) -> List[InjectionCase]:
    """The labelled benchmark. Default of 30 per family per the ticket."""
    cases: List[InjectionCase] = []
    for family in ALL_FAMILIES:
        cases.extend(_BUILDERS[family](per_family))
    return cases


def build_multi_fault_cases(n: int = 20) -> List[InjectionCase]:
    """Two simultaneous faults, to measure how gracefully accuracy degrades.

    There is no single right answer here; the assigner is expected to either name
    one of the two or report unidentifiable. What we measure is the drop, not
    correctness.
    """
    cases = []
    for i in range(n):
        cases.append(
            InjectionCase(
                case_id=f"multi-{i:03d}",
                family="multi",
                variant="memory+workflow",
                features=CreditFeatures(
                    outcome_verdict="failed",
                    outcome_confidence=0.9,
                    memory_conflicts_current_request=True,
                    memory_injected=2,
                    loop_stagnation=True,
                    step_order_suspect=True,
                ),
                expected_target=T_MEMORY,
            )
        )
    return cases


@dataclass
class BenchmarkReport:
    total: int = 0
    top1_correct: int = 0
    top2_correct: int = 0
    per_family: Dict[str, Dict[str, int]] = field(default_factory=dict)
    no_update_specificity: float = 0.0
    confound_recognition: float = 0.0
    pseudo_evolution_count: int = 0

    @property
    def top1_accuracy(self) -> float:
        return self.top1_correct / self.total if self.total else 0.0

    @property
    def top2_accuracy(self) -> float:
        return self.top2_correct / self.total if self.total else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "top1_accuracy": round(self.top1_accuracy, 4),
            "top2_accuracy": round(self.top2_accuracy, 4),
            "no_update_specificity": round(self.no_update_specificity, 4),
            "confound_recognition": round(self.confound_recognition, 4),
            # The headline safety number: how often a non-asset cause was
            # "fixed" by proposing an asset change.
            "pseudo_evolution_count": self.pseudo_evolution_count,
            "per_family": self.per_family,
        }


def run_benchmark(
    cases: Optional[List[InjectionCase]] = None,
    assigner: Callable[[CreditFeatures], Any] = assign_credit,
) -> BenchmarkReport:
    """Score an assigner against the labelled cases."""
    cases = cases if cases is not None else build_benchmark()
    report = BenchmarkReport(total=len(cases))

    null_total = null_correct = 0
    confound_total = confound_correct = 0

    for case in cases:
        decision = assigner(case.features)
        selected = decision.selected
        ranked = sorted(
            (decision.scores or {}).items(), key=lambda kv: kv[1], reverse=True
        )
        top2 = {name for name, _ in ranked[:2]} | {selected}

        family = report.per_family.setdefault(
            case.family, {"total": 0, "top1": 0, "top2": 0}
        )
        family["total"] += 1

        if case.is_correct(selected):
            report.top1_correct += 1
            family["top1"] += 1
        if case.expect_non_asset:
            confound_total += 1
            if selected in NON_ASSET_TARGETS:
                confound_correct += 1
            else:
                # A confound answered with an asset edit is pseudo-evolution.
                report.pseudo_evolution_count += 1
        if case.family == FAMILY_NULL:
            null_total += 1
            if selected == T_NO_UPDATE:
                null_correct += 1

        if case.expect_non_asset:
            if top2 & NON_ASSET_TARGETS:
                report.top2_correct += 1
                family["top2"] += 1
        elif case.expected_target in top2:
            report.top2_correct += 1
            family["top2"] += 1

    report.no_update_specificity = null_correct / null_total if null_total else 0.0
    report.confound_recognition = (
        confound_correct / confound_total if confound_total else 0.0
    )
    return report


# Gate thresholds. Deliberately not aspirational: these are the levels below
# which attribution is not fit to drive automated candidate generation.
GATE_TOP1_MIN = 0.80
GATE_NO_UPDATE_SPECIFICITY_MIN = 0.90
GATE_CONFOUND_RECOGNITION_MIN = 0.85
GATE_PSEUDO_EVOLUTION_MAX = 0


def evaluate_gate(report: BenchmarkReport) -> Tuple[bool, List[str]]:
    """Phase 1 exit gate. Returns (passed, list of failures)."""
    failures: List[str] = []
    if report.top1_accuracy < GATE_TOP1_MIN:
        failures.append(
            f"top1_accuracy {report.top1_accuracy:.3f} < {GATE_TOP1_MIN}"
        )
    if report.no_update_specificity < GATE_NO_UPDATE_SPECIFICITY_MIN:
        failures.append(
            f"no_update_specificity {report.no_update_specificity:.3f} "
            f"< {GATE_NO_UPDATE_SPECIFICITY_MIN}"
        )
    if report.confound_recognition < GATE_CONFOUND_RECOGNITION_MIN:
        failures.append(
            f"confound_recognition {report.confound_recognition:.3f} "
            f"< {GATE_CONFOUND_RECOGNITION_MIN}"
        )
    if report.pseudo_evolution_count > GATE_PSEUDO_EVOLUTION_MAX:
        failures.append(
            f"pseudo_evolution_count {report.pseudo_evolution_count} "
            f"> {GATE_PSEUDO_EVOLUTION_MAX}"
        )
    return (not failures), failures
