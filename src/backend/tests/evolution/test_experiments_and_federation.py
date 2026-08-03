"""GCE tickets 41–46 — experiment programme and candidate-only federation."""

import pytest

from core.evolution import experiments as EX
from core.evolution import federation as FD


# ── System matrix (41) ───────────────────────────────────────────────────────


def test_all_six_systems_are_defined():
    assert set(EX.SYSTEM_MATRIX) == set(EX.ALL_SYSTEMS)


def test_naive_joint_is_the_decisive_control():
    """B4 = "put three engines in one product", the claim under test."""
    b4 = EX.SYSTEM_MATRIX[EX.B4_NAIVE_JOINT]
    ours = EX.SYSTEM_MATRIX[EX.OURS_GCE]
    assert b4.memory and b4.skill and b4.workflow
    # The single difference that carries the contribution.
    assert b4.credit_assignment is False
    assert ours.credit_assignment is True


def test_static_baseline_evolves_nothing():
    b0 = EX.SYSTEM_MATRIX[EX.B0_STATIC]
    assert not any([b0.memory, b0.skill, b0.workflow, b0.credit_assignment])


def test_single_engine_baselines_isolate_one_engine_each():
    assert EX.SYSTEM_MATRIX[EX.B1_MEMORY_ONLY].memory is True
    assert EX.SYSTEM_MATRIX[EX.B1_MEMORY_ONLY].skill is False
    assert EX.SYSTEM_MATRIX[EX.B2_SKILL_ONLY].skill is True
    assert EX.SYSTEM_MATRIX[EX.B3_WORKFLOW_ONLY].workflow is True


def _run(system, **kw):
    base = dict(task_set_version="v1", base_model="m1", update_budget=10, updates_applied=10)
    base.update(kw)
    return EX.ExperimentRun(system=system, **base)


def test_arms_on_different_task_sets_are_not_comparable():
    ok, problems = EX.validate_comparable(
        [_run(EX.B4_NAIVE_JOINT), _run(EX.OURS_GCE, task_set_version="v2")]
    )
    assert ok is False and any("task_set_version" in p for p in problems)


def test_arms_on_different_base_models_are_not_comparable():
    ok, problems = EX.validate_comparable(
        [_run(EX.B4_NAIVE_JOINT), _run(EX.OURS_GCE, base_model="m2")]
    )
    assert ok is False and any("base_model" in p for p in problems)


def test_unequal_update_counts_are_rejected():
    """Otherwise 'changed more' reads as 'learned better'."""
    ok, problems = EX.validate_comparable(
        [_run(EX.B4_NAIVE_JOINT, updates_applied=30), _run(EX.OURS_GCE, updates_applied=10)]
    )
    assert ok is False and any("update counts" in p for p in problems)


def test_properly_matched_arms_are_comparable():
    ok, problems = EX.validate_comparable([_run(EX.B4_NAIVE_JOINT), _run(EX.OURS_GCE)])
    assert ok is True and problems == []


# ── Ablations (42) ───────────────────────────────────────────────────────────


def test_all_five_ablations_are_defined():
    assert len(EX.ALL_ABLATIONS) == 5


def test_the_safety_ablation_is_unreachable_outside_an_experiment():
    """Disabling an invariant is a research manoeuvre, not a config option."""
    ok, reason = EX.ablation_allowed(EX.ABL_NO_ONTOLOGY_INVARIANT, environment="production")
    assert ok is False and "isolated experiment" in reason
    assert EX.ablation_allowed(EX.ABL_NO_ONTOLOGY_INVARIANT, environment="experiment")[0] is True


def test_ordinary_ablations_are_allowed_anywhere():
    assert EX.ablation_allowed(EX.ABL_NO_SHADOW, environment="production")[0] is True


def test_unknown_ablation_is_rejected():
    assert EX.ablation_allowed("make_it_better", environment="experiment")[0] is False


# ── Longitudinal metrics (43) ────────────────────────────────────────────────


def test_forward_transfer_measures_unseen_task_types():
    assert EX.forward_transfer(unseen_after=0.75, unseen_baseline=0.60) == pytest.approx(0.15)


def test_catastrophic_forgetting_reports_the_worst_drop():
    value = EX.catastrophic_forgetting(
        before={"a": 0.9, "b": 0.8}, after={"a": 0.6, "b": 0.79}
    )
    assert value == pytest.approx(0.30)


def test_no_forgetting_when_nothing_dropped():
    assert EX.catastrophic_forgetting(before={"a": 0.9}, after={"a": 0.95}) == 0.0


def test_evolution_half_life_exposes_both_thrashing_and_stagnation():
    assert EX.evolution_half_life([1, 2, 3, 4, 5]) == 3.0
    assert EX.evolution_half_life([]) is None


def test_update_regret_is_undefined_without_activations():
    assert EX.update_regret(activated=0, later_rolled_back=0) is None
    assert EX.update_regret(activated=10, later_rolled_back=3) == pytest.approx(0.3)


def test_confidence_interval_is_wide_on_small_samples():
    """A point estimate on a small sample invites exactly the over-reading the
    programme is trying to avoid."""
    narrow = EX.wilson_interval(80, 100)
    wide = EX.wilson_interval(4, 5)
    assert (wide[1] - wide[0]) > (narrow[1] - narrow[0])


def test_confidence_interval_on_empty_sample():
    assert EX.wilson_interval(0, 0) == (0.0, 0.0)


# ── Non-stationary testbed (44) ──────────────────────────────────────────────


def test_all_four_perturbations_are_defined():
    assert len(EX.ALL_PERTURBATIONS) == 4


def test_schema_change_must_be_attributed_to_the_environment():
    """The direct empirical test of the pseudo-evolution defence."""
    assert EX.expected_attribution(EX.PERTURB_API_SCHEMA) == "environment"


def test_skill_availability_change_is_attributed_to_skill():
    assert EX.expected_attribution(EX.PERTURB_SKILL_AVAILABILITY) == "skill"


def test_domain_constraint_change_is_attributed_to_ontology():
    assert EX.expected_attribution(EX.PERTURB_DOMAIN_CONSTRAINT) == "ontology"


# ── Task sets (45) ───────────────────────────────────────────────────────────


def test_split_leakage_is_detected():
    """Leakage makes every downstream number meaningless while looking healthy."""
    task_set = EX.TaskSet(
        version="v1",
        splits={
            EX.SPLIT_TRAIN: ["t1", "t2"],
            EX.SPLIT_HOLDOUT: ["t2", "t3"],
            EX.SPLIT_GOLD: ["t9"],
        },
    )
    problems = task_set.isolation_violations()
    assert problems and "t2" in problems[0]


def test_clean_splits_report_no_violation():
    task_set = EX.TaskSet(
        version="v1",
        splits={EX.SPLIT_TRAIN: ["a"], EX.SPLIT_HOLDOUT: ["b"], EX.SPLIT_GOLD: ["c"]},
    )
    assert task_set.isolation_violations() == []


def test_sample_size_grows_as_the_target_effect_shrinks():
    big_effect = EX.required_sample_size(baseline_rate=0.6, mde=0.15)
    small_effect = EX.required_sample_size(baseline_rate=0.6, mde=0.05)
    # The benchmark size follows from the effect worth detecting, not the
    # other way round.
    assert small_effect > big_effect


def test_gold_split_refuses_model_graded_scoring():
    """A benchmark result a model graded itself on is not comparable."""
    ok, reason = EX.scoring_is_acceptable(uses_llm_judge=True, split=EX.SPLIT_GOLD)
    assert ok is False and "deterministic" in reason
    assert EX.scoring_is_acceptable(uses_llm_judge=False, split=EX.SPLIT_GOLD)[0] is True


# ── Federation (46) ──────────────────────────────────────────────────────────


def _shareable(domain="d1", signature="sig-a", effect=0.1):
    return FD.prepare_for_upload(
        signature=signature,
        target_kind="skill",
        operation="new",
        changes=[{"tool_sequence": ["kb_search", "export"]}],
        metrics={"effect_size": effect},
        domain_id=domain,
        upload_enabled=True,
    )


def test_raw_traces_are_never_uploadable():
    with pytest.raises(FD.UploadRefused) as e:
        FD.prepare_for_upload(
            signature="s",
            target_kind="skill",
            operation="new",
            changes=[],
            metrics={},
            domain_id="d1",
            upload_enabled=True,
            raw_trace={"messages": ["..."]},
        )
    # Accepting one "just this once" would make the guarantee untestable.
    assert e.value.code == "raw_trace_never_uploaded"


def test_local_environment_markers_block_the_upload():
    for leaky in (
        [{"path": "/Users/aaron/secret/report.xlsx"}],
        [{"path": "C:\\\\Users\\\\admin"}],
        [{"host": "192.168.1.20"}],
        [{"cfg": "api_key = sk-abc"}],
    ):
        with pytest.raises(FD.UploadRefused) as e:
            FD.prepare_for_upload(
                signature="s",
                target_kind="skill",
                operation="new",
                changes=leaky,
                metrics={},
                domain_id="d1",
                upload_enabled=True,
            )
        assert e.value.code == "local_markers_present"


def test_upload_disabled_by_the_domain_is_refused():
    with pytest.raises(FD.UploadRefused) as e:
        FD.prepare_for_upload(
            signature="s",
            target_kind="skill",
            operation="new",
            changes=[],
            metrics={},
            domain_id="d1",
            upload_enabled=False,
        )
    assert e.value.code == "upload_disabled"


def test_clean_candidate_uploads_with_a_one_way_origin_hash():
    candidate = _shareable()
    assert candidate.origin_hash and "d1" not in candidate.origin_hash


def test_low_support_candidates_are_not_distributed():
    """A candidate only one tenant produced would publish that tenant's specifics."""
    aggregated = FD.aggregate([_shareable("d1"), _shareable("d2")])
    assert aggregated[0].contributing_domains == 2
    assert aggregated[0].distributable is False
    assert FD.distributable(aggregated) == []


def test_sufficiently_supported_candidates_are_distributed():
    aggregated = FD.aggregate(
        [_shareable("d1"), _shareable("d2"), _shareable("d3"), _shareable("d4")]
    )
    assert aggregated[0].distributable is True
    assert len(FD.distributable(aggregated)) == 1


def test_duplicate_uploads_from_one_domain_do_not_inflate_support():
    aggregated = FD.aggregate([_shareable("d1"), _shareable("d1"), _shareable("d1")])
    # Distinct *domains*, not distinct submissions.
    assert aggregated[0].contributing_domains == 1


def test_the_centre_can_never_activate():
    assert FD.centre_can_activate() is False


def test_a_received_candidate_arrives_as_a_draft_needing_local_approval():
    aggregated = FD.aggregate([_shareable(f"d{i}") for i in range(4)])[0]
    result = FD.accept_downstream(aggregated, download_enabled=True)
    assert result["status"] == "draft"
    assert result["requires_local_replay"] is True
    assert result["requires_local_approval"] is True


def test_a_domain_can_refuse_downstream_candidates():
    aggregated = FD.aggregate([_shareable(f"d{i}") for i in range(4)])[0]
    assert FD.accept_downstream(aggregated, download_enabled=False)["accepted"] is False


def test_audit_records_all_three_directions():
    audit = FD.FederationAudit(uploaded=["a"], received=["b"], activated=["c"])
    payload = audit.to_dict()
    assert payload["uploaded"] and payload["received"] and payload["activated"]
