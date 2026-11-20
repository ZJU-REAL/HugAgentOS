"""GCE tickets 37 / 38 — multi-timescale activation limits and stability monitoring."""

from datetime import datetime, timedelta, timezone


from core.evolution.cycle import timescales as TS


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
