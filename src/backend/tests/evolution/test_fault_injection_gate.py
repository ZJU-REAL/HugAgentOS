"""GCE ticket 12 — the Phase 1 exit gate.

This file is a gate, not just a test. If it fails, attribution is not fit to
drive automated candidate generation and nothing downstream should be built on
it. Marked slow so it can be excluded from the fast inner loop while still
running in CI.
"""

import pytest

from core.evolution import fault_injection as FI
from core.evolution.credit import T_NO_UPDATE, T_SKILL, CreditDecision

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def report():
    return FI.run_benchmark(FI.build_benchmark(per_family=30))


# ── Benchmark construction ───────────────────────────────────────────────────


def test_every_family_is_represented_including_the_easy_to_forget_two():
    cases = FI.build_benchmark(per_family=30)
    families = {c.family for c in cases}
    assert families == set(FI.ALL_FAMILIES)
    # The confound bucket and the null control are what stop a system biased
    # toward "every failure needs some edit" from scoring perfectly.
    assert FI.FAMILY_CONFOUND in families
    assert FI.FAMILY_NULL in families


def test_thirty_cases_per_family_by_default():
    cases = FI.build_benchmark(per_family=30)
    for family in FI.ALL_FAMILIES:
        assert len([c for c in cases if c.family == family]) == 30


def test_each_family_covers_multiple_variants():
    cases = FI.build_benchmark(per_family=30)
    for family in (FI.FAMILY_MEMORY, FI.FAMILY_SKILL, FI.FAMILY_WORKFLOW):
        variants = {c.variant for c in cases if c.family == family}
        assert len(variants) >= 3


# ── The gate itself ──────────────────────────────────────────────────────────


def test_top1_accuracy_meets_the_gate(report):
    assert report.top1_accuracy >= FI.GATE_TOP1_MIN, report.to_dict()


def test_no_update_specificity_meets_the_gate(report):
    """Null controls must not provoke edits.

    This is the metric that catches a system which "always finds something to
    fix" — the most seductive failure mode in self-improving systems.
    """
    assert (
        report.no_update_specificity >= FI.GATE_NO_UPDATE_SPECIFICITY_MIN
    ), report.to_dict()


def test_confound_recognition_meets_the_gate(report):
    assert (
        report.confound_recognition >= FI.GATE_CONFOUND_RECOGNITION_MIN
    ), report.to_dict()


def test_zero_pseudo_evolution(report):
    """A KB gap / model miss / outage must never yield an asset candidate."""
    assert report.pseudo_evolution_count == FI.GATE_PSEUDO_EVOLUTION_MAX, report.to_dict()


def test_gate_verdict_is_pass(report):
    passed, failures = FI.evaluate_gate(report)
    assert passed, f"Phase 1 gate failed: {failures}\n{report.to_dict()}"


def test_per_family_accuracy_is_reported(report):
    for family in FI.ALL_FAMILIES:
        assert family in report.per_family
        assert report.per_family[family]["total"] == 30


# ── Degradation and gate integrity ───────────────────────────────────────────


def test_multi_fault_accuracy_is_measured_not_asserted():
    """With two simultaneous faults we record the drop rather than demand correctness."""
    multi = FI.run_benchmark(FI.build_multi_fault_cases(20))
    single = FI.run_benchmark(FI.build_benchmark(per_family=10))
    assert multi.total == 20
    # Degrading is expected and fine; the point is that it is measured.
    assert multi.top1_accuracy <= single.top1_accuracy + 1e-9


def test_a_degenerate_assigner_fails_the_gate():
    """Guards the gate itself: an assigner that always edits something must fail."""

    def always_skill(_features):
        return CreditDecision(selected=T_SKILL, confidence=0.99, scores={T_SKILL: 1.0})

    report = FI.run_benchmark(FI.build_benchmark(per_family=10), assigner=always_skill)
    passed, failures = FI.evaluate_gate(report)
    assert passed is False
    assert report.pseudo_evolution_count > 0
    assert report.no_update_specificity == 0.0


def test_an_always_no_update_assigner_also_fails_the_gate():
    """The opposite degenerate case: never learning anything."""

    def always_noop(_features):
        return CreditDecision(selected=T_NO_UPDATE, confidence=0.99, scores={})

    report = FI.run_benchmark(FI.build_benchmark(per_family=10), assigner=always_noop)
    passed, _ = FI.evaluate_gate(report)
    # Perfect specificity, useless accuracy — the gate must reject it.
    assert report.no_update_specificity == 1.0
    assert passed is False


def test_report_serialises_the_headline_safety_number():
    report = FI.run_benchmark(FI.build_benchmark(per_family=5))
    payload = report.to_dict()
    assert "pseudo_evolution_count" in payload
    assert "no_update_specificity" in payload
    assert "per_family" in payload
