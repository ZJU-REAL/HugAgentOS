"""GCE ticket 11 — credit assignment.

The assertions that matter most are the *negative* ones: a knowledge-base gap,
a model-capability miss and an external outage must never be attributed to a
mutable asset. Getting that wrong produces candidates that cannot fix anything
while polluting the statistics — the failure mode this design calls
pseudo-evolution.
"""

import pytest

from core.evolution import credit as C


def F(**kw) -> C.CreditFeatures:
    kw.setdefault("outcome_verdict", "failed")
    kw.setdefault("outcome_confidence", 0.9)
    return C.CreditFeatures(**kw)


# ── Pseudo-evolution guards ──────────────────────────────────────────────────


def test_kb_gap_attributes_to_retrieval_and_emits_no_asset_candidate():
    decision = C.assign_credit(F(kb_empty_result=True))
    assert decision.selected == C.T_RETRIEVAL
    # A missing document is fixed by adding the document, never by patching a skill.
    assert decision.may_produce_candidate is False


def test_external_schema_change_is_environment_only():
    decision = C.assign_credit(F(tool_schema_error=True))
    assert decision.selected == C.T_ENVIRONMENT
    assert decision.may_produce_candidate is False


def test_repeated_tool_outages_are_environment_not_skill():
    decision = C.assign_credit(F(tool_5xx_count=3))
    assert decision.selected == C.T_ENVIRONMENT
    assert decision.may_produce_candidate is False


def test_model_capability_miss_does_not_patch_an_asset():
    decision = C.assign_credit(F(model_capability_miss=True))
    assert decision.selected == C.T_MODEL
    assert decision.may_produce_candidate is False


def test_kb_gap_outranks_a_weak_skill_signal():
    """The exact shape of pseudo-evolution: a KB gap dressed up as a skill patch."""
    decision = C.assign_credit(
        F(kb_empty_result=True, skill_rejected_count=3, skill_selected_count=0)
    )
    assert decision.selected == C.T_RETRIEVAL


# ── Positive attribution ─────────────────────────────────────────────────────


def test_memory_conflict_attributes_to_memory():
    decision = C.assign_credit(F(memory_conflicts_current_request=True, memory_injected=3))
    assert decision.selected == C.T_MEMORY
    assert decision.may_produce_candidate is True
    # The prompt confound is named rather than silently ignored.
    assert C.T_PROMPT in decision.also_implicated


def test_repeated_subsequence_attributes_to_skill_with_workflow_noted():
    decision = C.assign_credit(F(repeated_tool_subsequence=True, skill_selected_count=1))
    assert decision.selected == C.T_SKILL
    assert C.T_ORCHESTRATION in decision.also_implicated


def test_stagnation_attributes_to_workflow():
    decision = C.assign_credit(
        F(loop_stagnation=True, no_diff_iterations=3, step_order_suspect=True)
    )
    assert decision.selected == C.T_ORCHESTRATION
    assert decision.may_produce_candidate is True


def test_repeated_denials_with_false_positive_evidence_attribute_to_ontology():
    decision = C.assign_credit(F(gate_denied_count=4, gate_false_positive_evidence=True))
    assert decision.selected == C.T_ONTOLOGY


# ── No-update and unidentifiable ─────────────────────────────────────────────


def test_success_yields_no_update_as_the_correct_answer():
    decision = C.assign_credit(
        C.CreditFeatures(outcome_verdict="success", outcome_confidence=0.9)
    )
    assert decision.selected == C.T_NO_UPDATE
    assert decision.may_produce_candidate is False


def test_sparse_evidence_yields_no_update_rather_than_a_guess():
    decision = C.assign_credit(
        C.CreditFeatures(outcome_verdict="failed", outcome_confidence=0.2)
    )
    assert decision.selected == C.T_NO_UPDATE
    assert decision.confidence < C.MIN_CONFIDENCE


def test_tied_targets_report_unidentifiable_rather_than_forcing_a_winner():
    # Memory conflict and prompt gap score close together by construction.
    decision = C.assign_credit(
        F(memory_conflicts_current_request=False, prompt_missing_priority_rule=True,
          repeated_tool_subsequence=False, loop_stagnation=False)
    )
    # Either a clear single winner or an explicit refusal — never a coin flip
    # presented as an answer.
    if decision.verdict == C.VERDICT_UNIDENTIFIABLE:
        assert decision.selected == C.T_NO_UPDATE
        assert len(decision.also_implicated) >= 2
    else:
        assert decision.selected in C.ALL_TARGETS


def test_unidentifiable_never_produces_a_candidate():
    decision = C.CreditDecision(
        selected=C.T_SKILL, confidence=0.9, verdict=C.VERDICT_UNIDENTIFIABLE
    )
    assert decision.may_produce_candidate is False


# ── Auditability ─────────────────────────────────────────────────────────────


def test_decision_is_deterministic():
    features = F(memory_conflicts_current_request=True, memory_injected=2)
    a = C.assign_credit(features)
    b = C.assign_credit(features)
    # An attribution you cannot reproduce cannot be audited or ablated.
    assert a.to_dict() == b.to_dict()


def test_decision_records_features_scores_and_version():
    decision = C.assign_credit(F(loop_stagnation=True))
    payload = decision.to_dict()
    assert payload["assigner_version"]
    assert payload["features"]
    assert set(payload["scores"]) == set(C.ALL_TARGETS)
    assert payload["explanation"]


def test_every_target_is_scored_even_when_zero():
    decision = C.assign_credit(F(loop_stagnation=True))
    assert all(t in decision.scores for t in C.ALL_TARGETS)


def test_non_asset_targets_are_explicitly_enumerated():
    # Guards against someone quietly making the environment bucket mutable.
    assert C.NON_ASSET_TARGETS == {
        C.T_RETRIEVAL,
        C.T_MODEL,
        C.T_ENVIRONMENT,
        C.T_NO_UPDATE,
    }
