"""GCE tickets 17 / 18 / 19 — release state machine, canary, guardrails.

Every assertion here is about *not* shipping something: illegal jumps, missing
verification, self-approval, unstable bucketing, missed rollbacks. Positive
paths are the easy half; the reason this module exists is the negative half.
"""

import pytest

from core.evolution import release as R


# ── State machine ────────────────────────────────────────────────────────────


def test_the_happy_path_is_legal_end_to_end():
    chain = [R.DRAFT, R.REPLAY_PASSED, R.SHADOW, R.CANARY, R.ACTIVE, R.RETIRED]
    for current, target in zip(chain, chain[1:]):
        assert R.is_legal_transition(current, target)


def test_draft_cannot_jump_straight_to_active():
    """The jump that would put an unverified change into production."""
    assert R.is_legal_transition(R.DRAFT, R.ACTIVE) is False
    with pytest.raises(R.ReleaseError) as e:
        R.assert_transition(R.DRAFT, R.ACTIVE)
    assert e.value.code == "illegal_transition"


def test_shadow_cannot_skip_canary():
    assert R.is_legal_transition(R.SHADOW, R.ACTIVE) is False


def test_terminal_stages_have_no_exits():
    for stage in R.TERMINAL_STAGES:
        assert R.TRANSITIONS[stage] == ()


def test_rollback_is_reachable_from_every_live_stage():
    for stage in (R.SHADOW, R.CANARY, R.ACTIVE):
        assert R.is_legal_transition(stage, R.ROLLED_BACK)


def test_a_rolled_back_release_cannot_be_resurrected():
    for target in (R.ACTIVE, R.CANARY, R.SHADOW):
        assert R.is_legal_transition(R.ROLLED_BACK, target) is False


# ── Verification gates ───────────────────────────────────────────────────────


def test_high_risk_requires_the_full_chain_before_active():
    ok, reason = R.can_promote(
        current_stage=R.CANARY,
        target_stage=R.ACTIVE,
        risk_tier="high",
        completed_verification=["replay", "shadow"],
        proposer="distiller",
        approver="admin",
    )
    assert ok is False
    assert "missing_verification" in reason and "canary" in reason


def test_promotion_to_canary_requires_shadow_first():
    ok, reason = R.can_promote(
        current_stage=R.REPLAY_PASSED,
        target_stage=R.CANARY,
        risk_tier="medium",
        completed_verification=["replay"],
        proposer="distiller",
        approver="admin",
    )
    assert ok is False and "shadow" in reason


def test_low_risk_still_requires_replay():
    """Nothing ships on a model's opinion alone, however low the risk."""
    ok, reason = R.can_promote(
        current_stage=R.CANARY,
        target_stage=R.ACTIVE,
        risk_tier="low",
        completed_verification=[],
        proposer="p",
        approver="a",
    )
    assert ok is False and "replay" in reason


def test_fully_verified_candidate_may_go_active():
    ok, reason = R.can_promote(
        current_stage=R.CANARY,
        target_stage=R.ACTIVE,
        risk_tier="high",
        completed_verification=["replay", "shadow", "canary", "human_approval"],
        proposer="distiller",
        approver="admin",
    )
    assert ok is True and reason == ""


# ── Separation of duties ─────────────────────────────────────────────────────


def test_a_generator_cannot_approve_its_own_candidate():
    ok, reason = R.can_promote(
        current_stage=R.CANARY,
        target_stage=R.ACTIVE,
        risk_tier="low",
        completed_verification=["replay"],
        proposer="skill_distiller",
        approver="skill_distiller",
    )
    # The structural form of "a learner cannot be its own judge".
    assert ok is False and reason == "self_approval_forbidden"


def test_activation_without_an_approver_is_refused():
    ok, reason = R.can_promote(
        current_stage=R.CANARY,
        target_stage=R.ACTIVE,
        risk_tier="low",
        completed_verification=["replay"],
        proposer="p",
        approver=None,
    )
    assert ok is False and reason == "approval_required"


# ── Canary bucketing ─────────────────────────────────────────────────────────


def test_bucketing_is_stable_for_a_subject_within_a_release():
    args = dict(release_id="rel-1", subject_id="user-42", traffic_percent=25)
    first = R.in_canary_bucket(**args)
    # A user flip-flopping between versions is both a bad experience and a
    # broken experiment.
    assert all(R.in_canary_bucket(**args) is first for _ in range(50))


def test_bucketing_differs_across_releases_for_the_same_subject():
    """Otherwise the same unlucky cohort receives every experiment."""
    assignments = {
        R.in_canary_bucket(release_id=f"rel-{i}", subject_id="user-42", traffic_percent=50)
        for i in range(30)
    }
    assert assignments == {True, False}


def test_traffic_percentage_is_approximately_honoured():
    hits = sum(
        R.in_canary_bucket(release_id="rel-1", subject_id=f"u{i}", traffic_percent=25)
        for i in range(2000)
    )
    assert 0.20 <= hits / 2000 <= 0.30


def test_zero_and_full_traffic_are_absolute():
    assert R.in_canary_bucket(release_id="r", subject_id="u", traffic_percent=0) is False
    assert R.in_canary_bucket(release_id="r", subject_id="u", traffic_percent=100) is True


def test_missing_subject_never_enters_the_bucket():
    # An anonymous request cannot be assigned stably, so it stays on the old版本.
    assert R.in_canary_bucket(release_id="r", subject_id="", traffic_percent=99) is False


def test_ramp_steps_start_small():
    assert R.next_traffic(0) == 5
    assert R.next_traffic(5) == 25
    assert R.next_traffic(25) == 100
    assert R.next_traffic(100) == 100


# ── Guardrails ───────────────────────────────────────────────────────────────


def test_higher_is_worse_metrics_breach_when_exceeded():
    breaches = R.evaluate_guardrails(
        {"failure_rate": 0.12, "p95_latency_ms": 900},
        {"failure_rate": 0.05, "p95_latency_ms": 2000},
    )
    assert [b.metric for b in breaches] == ["failure_rate"]


def test_lower_is_worse_metrics_breach_when_undershot():
    breaches = R.evaluate_guardrails({"success_rate": 0.55}, {"success_rate": 0.80})
    assert breaches and breaches[0].metric == "success_rate"


def test_metric_direction_is_not_inferred():
    """A sign error here would either roll back everything or nothing."""
    # A high success rate must never read as a breach.
    assert R.evaluate_guardrails({"success_rate": 0.99}, {"success_rate": 0.80}) == []
    # A low failure rate must never read as a breach.
    assert R.evaluate_guardrails({"failure_rate": 0.001}, {"failure_rate": 0.05}) == []


def test_unknown_metrics_are_ignored_rather_than_guessed():
    assert R.evaluate_guardrails({"vibes": 3.0}, {"vibes": 1.0}) == []


def test_any_single_breach_triggers_rollback():
    breaches = R.evaluate_guardrails({"safety_violations": 1}, {"safety_violations": 0})
    # No "two out of three" softening — each guardrail is already the level at
    # which the release is unacceptable.
    assert R.should_auto_rollback(breaches) is True


def test_no_breach_means_no_rollback():
    assert R.should_auto_rollback([]) is False


def test_breach_serialises_enough_to_diagnose():
    breach = R.evaluate_guardrails(
        {"denial_rate": 0.4}, {"denial_rate": 0.1}, window="5m"
    )[0]
    payload = breach.to_dict()
    assert payload["observed"] == 0.4 and payload["threshold"] == 0.1
    assert payload["window"] == "5m"
