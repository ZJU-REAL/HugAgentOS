"""GCE tickets 28 / 30 / 31 / 37 / 38 / 39 / 40 — the remaining core mechanisms."""

from datetime import datetime, timedelta, timezone

import pytest

from core.evolution import counterfactual as CF
from core.evolution import evaluator as EV
from core.evolution import timescales as TS


# ── Counterfactual attribution (28) ──────────────────────────────────────────


def test_removing_the_culprit_makes_a_failing_task_pass():
    probes = [
        CF.CounterfactualProbe("memory", "stale-pref", CF.MODE_REMOVE),
        CF.CounterfactualProbe("skill", "report", CF.MODE_REMOVE),
    ]

    def run_probe(probe):
        # Baseline fails; removing the memory fixes it, removing the skill does not.
        if probe is None:
            return False
        return probe.asset_kind == "memory"

    result = CF.run_counterfactual_attribution(probes, run_probe)
    assert result.verdict == "memory"
    assert result.credits["memory"] > 0


def test_when_nothing_changes_the_cause_is_outside_the_mutable_assets():
    probes = [CF.CounterfactualProbe("memory", "m", CF.MODE_REMOVE)]
    result = CF.run_counterfactual_attribution(probes, lambda p: False)
    # Saying "the cause is not something we can edit" is the useful answer.
    assert result.verdict == "no_update"
    assert any("non-asset" in note for note in result.notes)


def test_multiple_implicated_assets_report_unidentifiable():
    probes = [
        CF.CounterfactualProbe("memory", "m", CF.MODE_REMOVE),
        CF.CounterfactualProbe("workflow", "w", CF.MODE_REMOVE),
    ]
    result = CF.run_counterfactual_attribution(probes, lambda p: p is not None)
    assert result.verdict == "unidentifiable"


def test_an_asset_that_was_helping_gets_negative_credit():
    probes = [CF.CounterfactualProbe("ontology", "gate", CF.MODE_REMOVE)]

    def run_probe(probe):
        # Baseline passes; removing the gate makes it fail ⇒ the gate protects.
        return probe is None

    result = CF.run_counterfactual_attribution(probes, run_probe)
    assert result.credits["ontology"] < 0


def test_repeats_defend_against_reading_noise_as_causation():
    calls = {"n": 0}

    def flaky(probe):
        calls["n"] += 1
        return calls["n"] % 2 == 0

    CF.run_counterfactual_attribution(
        [CF.CounterfactualProbe("memory", "m")], flaky, repeats=3
    )
    # Baseline 3 + probe 3 — a single differing run is not treated as evidence.
    assert calls["n"] == 6


def test_the_honest_boundary_travels_with_every_verdict():
    result = CF.run_counterfactual_attribution(
        [CF.CounterfactualProbe("memory", "m")], lambda p: True
    )
    assert "非随机化因果证明" in result.boundary_note


def test_no_probes_is_not_an_error():
    assert CF.run_counterfactual_attribution([], lambda p: True).verdict == "no_update"


# ── Decision objective (30) ──────────────────────────────────────────────────


def test_a_risky_marginal_change_ranks_below_a_safe_one():
    risky = {"id": "risky", "terms": CF.DecisionTerms(0.06, 0.5, 0.7, 0.8)}
    safe = {"id": "safe", "terms": CF.DecisionTerms(0.05, 0.05, 0.1, 0.1)}
    ranked = CF.rank_candidates([risky, safe])
    # Without the penalty terms these would rank equal, and the replay budget
    # would go to changes reviewers reject anyway.
    assert ranked[0]["id"] == "safe"


def test_low_value_candidates_are_filtered_before_replay():
    poor = {"id": "poor", "terms": CF.DecisionTerms(0.01, 0.9, 0.9, 0.9)}
    assert CF.rank_candidates([poor], min_score=0.0) == []


def test_ranking_without_a_filter_keeps_everything_ordered():
    """A batch where nothing scores positive is still meaningfully ordered."""
    poor = {"id": "poor", "terms": CF.DecisionTerms(0.01, 0.9, 0.9, 0.9)}
    better = {"id": "better", "terms": CF.DecisionTerms(0.02, 0.5, 0.5, 0.5)}
    ranked = CF.rank_candidates([poor, better])
    assert [c["id"] for c in ranked] == ["better", "poor"]


def test_cost_is_measured_not_constant():
    cheap = CF.measured_cost(replay_tokens=1000, latency_delta_ms=0, token_budget=1_000_000)
    dear = CF.measured_cost(replay_tokens=900_000, latency_delta_ms=4000, token_budget=1_000_000)
    assert dear > cheap


def test_risk_grows_with_tier_and_history():
    low = CF.measured_risk(risk_tier="low", historical_rollback_rate=0.0)
    high = CF.measured_risk(risk_tier="high", historical_rollback_rate=0.4)
    assert high > low


def test_complexity_grows_with_dependency_breadth():
    assert CF.measured_complexity(dependent_assets=9) > CF.measured_complexity(
        dependent_assets=1
    )


# ── Replay/canary calibration (31) ───────────────────────────────────────────


def test_calibration_reports_screening_precision_and_recall():
    paired = (
        [{"kind": "skill", "replay_improved": True, "canary_improved": True}] * 8
        + [{"kind": "skill", "replay_improved": True, "canary_improved": False}] * 4
        + [{"kind": "skill", "replay_improved": False, "canary_improved": True}] * 2
        + [{"kind": "skill", "replay_improved": False, "canary_improved": False}] * 10
    )
    report = CF.calibrate_replay_against_canary(paired)
    assert report.precision == pytest.approx(8 / 12)
    assert report.recall == pytest.approx(8 / 10)
    assert report.sufficient is True


def test_poor_precision_recommends_a_tighter_threshold():
    paired = [{"kind": "skill", "replay_improved": True, "canary_improved": False}] * 25
    assert (
        CF.calibrate_replay_against_canary(paired).recommendation()
        == "tighten_replay_threshold"
    )


def test_poor_recall_recommends_loosening():
    """Over-strict screening silently discards real improvements.

    Note the screen made *no* positive calls here, so precision is 0/0. Reading
    that as "poor precision" would recommend tightening a filter that already
    rejects everything — precisely the wrong direction.
    """
    paired = [{"kind": "skill", "replay_improved": False, "canary_improved": True}] * 25
    assert (
        CF.calibrate_replay_against_canary(paired).recommendation()
        == "loosen_replay_threshold"
    )


def test_small_calibration_samples_refuse_a_recommendation():
    report = CF.calibrate_replay_against_canary(
        [{"kind": "skill", "replay_improved": True, "canary_improved": True}] * 3
    )
    assert report.recommendation() == "insufficient_samples"


def test_calibration_records_that_the_effect_size_belongs_to_canary():
    report = CF.calibrate_replay_against_canary([])
    assert "主效应量来自灰度" in report.to_dict()["note"]


# ── Multi-timescale (37) ─────────────────────────────────────────────────────


def _now():
    return datetime(2026, 7, 29, tzinfo=timezone.utc)


def test_cooldown_blocks_a_second_activation_in_the_same_layer():
    limiter = TS.RateLimiter()
    limiter.record_activation(TS.LAYER_SKILL, now=_now())
    ok, reason = limiter.may_activate(TS.LAYER_SKILL, now=_now() + timedelta(minutes=5))
    assert ok is False and reason.startswith("cooldown")


def test_memory_moves_far_faster_than_skills():
    limiter = TS.RateLimiter()
    limiter.record_activation(TS.LAYER_MEMORY, now=_now())
    ok, _ = limiter.may_activate(TS.LAYER_MEMORY, now=_now() + timedelta(minutes=5))
    assert ok is True


def test_a_fast_layer_waits_while_a_slower_one_is_in_flight():
    limiter = TS.RateLimiter()
    limiter.in_flight[TS.LAYER_ONTOLOGY] = True
    ok, reason = limiter.may_activate(TS.LAYER_MEMORY, now=_now())
    # Cross-layer amplification is how a memory tweak ends up rewriting the
    # domain model.
    assert ok is False and "slower_layer_in_flight" in reason


def test_a_slow_layer_is_not_blocked_by_a_faster_one():
    limiter = TS.RateLimiter()
    limiter.in_flight[TS.LAYER_MEMORY] = True
    ok, _ = limiter.may_activate(TS.LAYER_ONTOLOGY, now=_now())
    assert ok is True


def test_concurrent_activation_cap_is_enforced():
    limiter = TS.RateLimiter()
    ok, reason = limiter.may_activate(
        TS.LAYER_MEMORY, now=_now(), concurrent_activations=3
    )
    assert ok is False and reason == "too_many_concurrent_activations"


def test_global_freeze_stops_every_layer():
    limiter = TS.RateLimiter()
    limiter.freeze()
    for layer in TS.LAYER_ORDER:
        ok, reason = limiter.may_activate(layer, now=_now())
        assert ok is False and reason == "globally_frozen"


def test_tenant_overrides_apply():
    limiter = TS.RateLimiter(tenant_overrides={"acme": {TS.LAYER_SKILL: 1}})
    limiter.record_activation(TS.LAYER_SKILL, now=_now())
    ok, _ = limiter.may_activate(
        TS.LAYER_SKILL, now=_now() + timedelta(seconds=5), tenant="acme"
    )
    assert ok is True


# ── Stability monitoring (38) ────────────────────────────────────────────────


def test_gold_standard_regression_freezes_activations():
    """The degradation no single release guardrail can see."""
    report = TS.evaluate_drift(
        gold_results=[TS.GoldStandardResult(f"g{i}", i < 7) for i in range(10)],
        baseline_score=0.90,
        task_type_before={},
        task_type_after={},
        activations_in_window=["a1", "a2"],
    )
    assert report.should_freeze is True
    assert any("gold_standard_regression" in a for a in report.alerts)


def test_a_healthy_gold_run_does_not_freeze():
    report = TS.evaluate_drift(
        gold_results=[TS.GoldStandardResult(f"g{i}", True) for i in range(10)],
        baseline_score=0.95,
        task_type_before={},
        task_type_after={},
        activations_in_window=[],
    )
    assert report.should_freeze is False and report.alerts == []


def test_forgetting_is_detected_per_task_type():
    report = TS.evaluate_drift(
        gold_results=[],
        baseline_score=0.0,
        task_type_before={"kb_qa": 0.90, "db_analysis": 0.80},
        task_type_after={"kb_qa": 0.70, "db_analysis": 0.79},
        activations_in_window=[],
    )
    assert report.forgotten_task_types == ["kb_qa"]


def test_version_churn_is_reported():
    report = TS.evaluate_drift(
        gold_results=[],
        baseline_score=0.0,
        task_type_before={},
        task_type_after={},
        activations_in_window=[f"a{i}" for i in range(8)],
        window_capacity=10,
    )
    assert any("version_churn" in a for a in report.alerts)


def test_alerts_carry_the_recent_activations_for_diagnosis():
    report = TS.evaluate_drift(
        gold_results=[TS.GoldStandardResult("g", False)],
        baseline_score=1.0,
        task_type_before={},
        task_type_after={},
        activations_in_window=["skill-a", "workflow-b"],
    )
    assert report.recent_activations == ["skill-a", "workflow-b"]


def test_bulk_rollback_targets_a_whole_bundle():
    plan = TS.bulk_rollback_plan(["a", "b", "c"], "bundle_good")
    # Per-asset rollback cannot repair damage caused by the combination.
    assert plan["kind"] == "bulk" and plan["target_bundle_id"] == "bundle_good"


def test_the_gold_set_itself_is_protected():
    """Otherwise the cheapest fix for a failing score is to change the questions."""
    assert TS.gold_set_is_protected(actor_is_privileged=False, audited=True)[0] is False
    assert TS.gold_set_is_protected(actor_is_privileged=True, audited=False)[0] is False
    assert TS.gold_set_is_protected(actor_is_privileged=True, audited=True)[0] is True


# ── Evaluator committee (39) ─────────────────────────────────────────────────


def _j(lens, verdict, score, judge="judge-a", **kw):
    return EV.Judgement(lens=lens, verdict=verdict, score=score, judge_id=judge, **kw)


def test_a_proposer_cannot_judge_its_own_candidate():
    with pytest.raises(EV.SelfJudgementError):
        EV.convene(
            [_j(EV.LENS_QUALITY, EV.VERDICT_APPROVE, 0.9, judge="distiller")],
            proposer="distiller",
        )


def test_strong_disagreement_escalates_rather_than_averaging():
    result = EV.convene(
        [
            _j(EV.LENS_QUALITY, EV.VERDICT_APPROVE, 0.95, judge="a"),
            _j(EV.LENS_RISK, EV.VERDICT_REJECT, 0.10, judge="b"),
        ]
    )
    # Averaging would conflate "strongly opposed" with "both lukewarm".
    assert result.escalated is True and "disagreement_spread" in result.escalation_reason


def test_agreement_produces_a_verdict():
    result = EV.convene(
        [
            _j(EV.LENS_QUALITY, EV.VERDICT_APPROVE, 0.85, judge="a"),
            _j(EV.LENS_COST, EV.VERDICT_APPROVE, 0.80, judge="b"),
        ]
    )
    assert result.verdict == EV.VERDICT_APPROVE and result.escalated is False


def test_a_deterministic_rejection_is_final():
    result = EV.convene(
        [
            _j(EV.LENS_QUALITY, EV.VERDICT_APPROVE, 0.9, judge="a"),
            _j(EV.LENS_RISK, EV.VERDICT_REJECT, 0.85, judge="verifier", deterministic=True),
        ]
    )
    # A verifier's finding is a fact, not an opinion to be outvoted.
    assert result.verdict == EV.VERDICT_REJECT


def test_high_risk_cannot_be_judged_solely_by_the_generator_model():
    result = EV.convene(
        [
            _j(EV.LENS_QUALITY, EV.VERDICT_APPROVE, 0.9, judge="a", model="gen-model"),
            _j(EV.LENS_COST, EV.VERDICT_APPROVE, 0.88, judge="b", model="gen-model"),
        ],
        risk_tier="high",
        generator_model="gen-model",
    )
    # A shared model shares blind spots, so its agreement proves little.
    assert result.escalated is True
    assert result.escalation_reason == "high_risk_requires_independent_verifier"


def test_an_evaluator_outage_fails_closed():
    result = EV.evaluator_failure_verdict()
    # Treating silence as consent turns an outage into an unreviewed release.
    assert result.verdict == EV.VERDICT_REJECT and result.escalated is True


def test_no_judgements_escalates():
    assert EV.convene([]).escalated is True


# ── Monotone safety (40) ─────────────────────────────────────────────────────


def _checks():
    return EV.SafetyCheckSet(
        checks={"citation_required", "no_pii"},
        thresholds={"max_failure_rate": 0.05},
        stricter_is_higher=set(),
    )


def test_automation_may_add_checks():
    updated = EV.apply_automated_change(_checks(), add_checks=["no_secrets"])
    assert "no_secrets" in updated.checks and len(updated.checks) == 3


def test_automation_cannot_remove_a_check_at_all():
    with pytest.raises(EV.MonotonicityViolation) as e:
        EV.apply_automated_change(_checks(), remove_checks=["no_pii"])
    # Unreachable, not merely discouraged.
    assert e.value.code == "removal_not_reachable_by_automation"


def test_relaxing_a_threshold_counts_as_removal():
    """Otherwise 'tighten to meaninglessness' is an open back door."""
    with pytest.raises(EV.MonotonicityViolation) as e:
        EV.apply_automated_change(_checks(), new_thresholds={"max_failure_rate": 0.5})
    assert e.value.code == "threshold_relaxation_is_removal"


def test_tightening_a_threshold_is_allowed():
    updated = EV.apply_automated_change(
        _checks(), new_thresholds={"max_failure_rate": 0.01}
    )
    assert updated.thresholds["max_failure_rate"] == 0.01


def test_human_removal_requires_actor_reason_and_audit():
    for kwargs in (
        dict(actor="", reason="r", audited=True),
        dict(actor="admin", reason="", audited=True),
        dict(actor="admin", reason="r", audited=False),
    ):
        with pytest.raises(EV.MonotonicityViolation):
            EV.apply_human_removal(_checks(), remove_checks=["no_pii"], **kwargs)


def test_a_complete_human_removal_succeeds_and_alerts():
    updated, record = EV.apply_human_removal(
        _checks(), remove_checks=["no_pii"], actor="admin", reason="法务确认", audited=True
    )
    assert "no_pii" not in updated.checks
    assert record["alert"] is True and record["actor"] == "admin"


def test_monotonicity_self_check_detects_a_lost_check():
    """A one-off design guarantee decays; this keeps it true."""
    history = [
        EV.SafetyCheckSet(checks={"a", "b"}),
        EV.SafetyCheckSet(checks={"a"}),
    ]
    ok, problems = EV.verify_monotonicity(history)
    assert ok is False and problems


def test_monotonicity_self_check_passes_on_a_growing_set():
    history = [
        EV.SafetyCheckSet(checks={"a"}),
        EV.SafetyCheckSet(checks={"a", "b"}),
    ]
    assert EV.verify_monotonicity(history)[0] is True
