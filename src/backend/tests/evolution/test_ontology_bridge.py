"""GCE ticket 22 — ontology drafts on the shared ledger.

The bridge flows one way: ontology adopts the shared ledger and gives up
nothing. These tests exist to make sure a later change cannot quietly grant
ontology one of the framework's faster paths.
"""

import pytest

from core.evolution import ontology_bridge as OB


def _candidate(**kw):
    base = dict(
        draft_id="ontod_1",
        pack_id="enterprise",
        operation="false_positive",
        proposal={},
    )
    base.update(kw)
    return OB.OntologyCandidate(**base)


# ── The invariant ────────────────────────────────────────────────────────────


def test_ontology_candidates_are_never_auto_activatable():
    assert _candidate().auto_activatable is False


def test_activation_requires_a_human_regardless_of_evidence():
    candidate = _candidate(denial_count=50, user_corrections=30)
    ok, reason = OB.activation_is_permitted(candidate, approver=None)
    # No evidence threshold unlocks automation here.
    assert ok is False and reason == "human_approval_required"


def test_activation_permitted_with_an_approver():
    ok, reason = OB.activation_is_permitted(_candidate(), approver="admin")
    assert ok is True and reason == ""


def test_candidates_are_always_highest_risk_tier():
    payload = _candidate().to_ir_payload()
    # Ontology binds every other asset — a mistake is not contained to one skill.
    assert payload["risk_tier"] == "high"


def test_new_constraints_start_in_log_only_mode():
    assert _candidate().mode == OB.MODE_LOG_ONLY
    assert _candidate().to_ir_payload()["mode"] == OB.MODE_LOG_ONLY


# ── Consistency checks ───────────────────────────────────────────────────────


def test_dangling_concept_is_rejected():
    ok, problems = OB.check_consistency(
        _candidate(proposal={"concept": "不存在"}),
        known_concepts=["企业主体"],
        referenced_tools=[],
        existing_workflows=[],
    )
    assert ok is False
    assert problems[0].code == "dangling_concept"


def test_unknown_tool_reference_is_rejected():
    ok, problems = OB.check_consistency(
        _candidate(proposal={"tools": ["ghost_tool"]}),
        known_concepts=[],
        referenced_tools=["company_verify"],
        existing_workflows=[],
    )
    assert ok is False and problems[0].code == "unknown_tool"


def test_a_candidate_that_breaks_another_workflow_is_rejected():
    ok, problems = OB.check_consistency(
        _candidate(proposal={"breaks_workflows": ["fin_report"]}),
        known_concepts=[],
        referenced_tools=[],
        existing_workflows=["fin_report"],
    )
    assert ok is False and problems[0].code == "breaks_workflow"


def test_hard_constraints_cannot_be_weakened_by_any_candidate():
    ok, problems = OB.check_consistency(
        _candidate(proposal={"weakens_constraint": "citation_required"}),
        known_concepts=[],
        referenced_tools=[],
        existing_workflows=[],
        hard_constraints=["citation_required"],
    )
    assert ok is False and problems[0].code == "hard_constraint_weakened"


def test_a_clean_candidate_passes():
    ok, problems = OB.check_consistency(
        _candidate(proposal={"concept": "内部流程草稿"}),
        known_concepts=["内部流程草稿"],
        referenced_tools=[],
        existing_workflows=[],
    )
    assert ok is True and problems == []


# ── Evidence aggregation ─────────────────────────────────────────────────────


def test_denials_alone_are_not_enough_evidence():
    """Repeated denials may simply mean the rule is doing its job."""
    packet = OB.aggregate_false_positive_evidence(
        denials=[{"rule_id": "r1", "event_id": f"e{i}"} for i in range(5)],
        corrections=[],
    )
    assert packet is None


def test_denials_plus_human_corrections_form_an_evidence_packet():
    packet = OB.aggregate_false_positive_evidence(
        denials=[{"rule_id": "r1", "event_id": f"e{i}"} for i in range(5)],
        corrections=[{"rule_id": "r1"}, {"rule_id": "r1"}, {"rule_id": "r1"}],
    )
    # It is the human corrections that turn "the gate fired a lot" into
    # "the gate is wrong".
    assert packet is not None
    assert packet["rule_id"] == "r1"
    assert packet["denial_count"] == 5 and packet["correction_count"] == 3


def test_too_few_denials_produce_no_packet():
    packet = OB.aggregate_false_positive_evidence(
        denials=[{"rule_id": "r1", "event_id": "e1"}],
        corrections=[{"rule_id": "r1"}],
    )
    assert packet is None


# ── Log-only → enforce ───────────────────────────────────────────────────────


def test_too_few_observations_cannot_promote_to_enforce():
    ok, reason = OB.promote_to_enforce(observations=5, false_positives=0)
    assert ok is False and "observations" in reason


def test_high_false_positive_rate_recommends_withdrawal_not_enforcement():
    ok, reason = OB.promote_to_enforce(observations=100, false_positives=30)
    assert ok is False and "撤回" in reason


def test_a_well_behaved_constraint_may_enforce():
    ok, reason = OB.promote_to_enforce(observations=100, false_positives=2)
    assert ok is True and reason == ""


def test_zero_observations_never_promotes():
    ok, _ = OB.promote_to_enforce(observations=0, false_positives=0)
    assert ok is False
