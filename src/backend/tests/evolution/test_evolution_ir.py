"""GCE ticket 13 — Evolution IR and the invariant gate.

Everything asserted here is a security property. The gate's job is to stop
self-evolution becoming a privilege-escalation path; quality judgements happen
later in the pipeline.
"""

import pytest

from core.evolution import ir as IR


def _credit(ok=True):
    return {"may_produce_candidate": ok, "selected": "skill", "confidence": 0.9}


def _ir(**kw):
    base = dict(
        target_kind="skill",
        target_asset_id="industry-report",
        base_version="v1",
        operation="patch",
        rollback=IR.Rollback(version_id="v1", triggers=["success -3pp"]),
    )
    base.update(kw)
    return IR.EvolutionIR(**base)


# ── Operation white-list ─────────────────────────────────────────────────────


def test_unknown_asset_kind_is_rejected():
    with pytest.raises(IR.InvariantViolation) as e:
        IR.validate_ir(_ir(target_kind="whatever"), credit_decision=_credit())
    assert e.value.code == "unknown_kind"


def test_operation_outside_the_whitelist_is_rejected():
    with pytest.raises(IR.InvariantViolation) as e:
        IR.validate_ir(_ir(operation="delete_everything"), credit_decision=_credit())
    assert e.value.code == "operation_not_allowed"


def test_workflow_cannot_request_source_edits():
    """Arbitrary self-modifying code is the one capability refused outright."""
    for forbidden in ("edit_source", "exec", "patch_code", "eval"):
        with pytest.raises(IR.InvariantViolation):
            IR.validate_ir(
                _ir(target_kind="workflow", operation=forbidden, base_version="wf1"),
                credit_decision=_credit(),
            )


def test_workflow_declarative_operations_are_allowed():
    for allowed in ("route", "dag", "budget", "reviewer", "rollback"):
        IR.validate_ir(
            _ir(target_kind="workflow", operation=allowed), credit_decision=_credit()
        )


# ── Privilege escalation ─────────────────────────────────────────────────────


def test_widening_tool_authority_is_rejected():
    with pytest.raises(IR.InvariantViolation) as e:
        IR.validate_ir(
            _ir(requested_tools=["read", "write", "bash"]),
            base_tools=["read", "write"],
            credit_decision=_credit(),
        )
    assert e.value.code == "tool_escalation"


def test_narrowing_or_matching_tool_authority_is_allowed():
    IR.validate_ir(
        _ir(requested_tools=["read"]),
        base_tools=["read", "write"],
        credit_decision=_credit(),
    )


def test_scope_escalation_is_rejected():
    """A candidate learned in one tenant must not silently generalise."""
    with pytest.raises(IR.InvariantViolation) as e:
        IR.validate_ir(
            _ir(scope={"level": "global"}),
            allowed_scopes=["user", "workspace"],
            credit_decision=_credit(),
        )
    assert e.value.code == "scope_escalation"


# ── Accountability ───────────────────────────────────────────────────────────


def test_candidate_without_a_credit_decision_is_rejected():
    with pytest.raises(IR.InvariantViolation) as e:
        IR.validate_ir(_ir(), credit_decision=None)
    assert e.value.code == "missing_credit"


def test_credit_decision_that_forbids_candidates_blocks_the_ir():
    """A KB gap / low-confidence / unidentifiable verdict cannot produce an edit."""
    with pytest.raises(IR.InvariantViolation) as e:
        IR.validate_ir(_ir(), credit_decision=_credit(ok=False))
    assert e.value.code == "credit_forbids_candidate"


def test_missing_rollback_target_is_rejected():
    with pytest.raises(IR.InvariantViolation) as e:
        IR.validate_ir(_ir(rollback=IR.Rollback()), credit_decision=_credit())
    assert e.value.code == "missing_rollback"


# ── Ontology invariants ──────────────────────────────────────────────────────


def test_ontology_candidate_cannot_self_activate():
    with pytest.raises(IR.InvariantViolation) as e:
        IR.validate_ir(
            _ir(target_kind="ontology", operation="activate"), credit_decision=_credit()
        )
    assert e.value.code == "ontology_self_activation"


def test_dangling_concept_reference_is_rejected():
    with pytest.raises(IR.InvariantViolation) as e:
        IR.validate_ir(
            _ir(
                target_kind="ontology",
                operation="constraint",
                changes=[{"concept": "不存在的概念"}],
            ),
            credit_decision=_credit(),
            known_concepts=["企业主体", "风险报告"],
        )
    assert e.value.code == "dangling_concept"


# ── De-duplication ───────────────────────────────────────────────────────────


def test_same_change_from_different_evidence_has_the_same_checksum():
    a = _ir(changes=[{"path": "steps.order", "before": 3, "after": 1}], evidence_refs=["ep1"])
    b = _ir(changes=[{"path": "steps.order", "before": 3, "after": 1}], evidence_refs=["ep2"])
    # Repeated evidence must not flood the review queue with the same edit.
    assert a.change_checksum() == b.change_checksum()


def test_different_changes_have_different_checksums():
    a = _ir(changes=[{"path": "steps.order", "after": 1}])
    b = _ir(changes=[{"path": "steps.order", "after": 2}])
    assert a.change_checksum() != b.change_checksum()


def test_checksum_ignores_creation_time():
    a = _ir(changes=[{"x": 1}])
    b = _ir(changes=[{"x": 1}])
    b.created_at = "2099-01-01T00:00:00+00:00"
    assert a.change_checksum() == b.change_checksum()


# ── Risk tiers ───────────────────────────────────────────────────────────────


def test_risk_tier_determines_minimum_verification():
    assert "human_approval" in IR.required_verification(IR.RISK_HIGH)
    assert "shadow" in IR.required_verification(IR.RISK_MEDIUM)
    # Low risk still requires replay — nothing ships purely on a model's opinion.
    assert "replay" in IR.required_verification(IR.RISK_LOW)


def test_unknown_risk_tier_is_rejected():
    with pytest.raises(IR.InvariantViolation) as e:
        IR.validate_ir(_ir(risk_tier="trivial"), credit_decision=_credit())
    assert e.value.code == "bad_risk_tier"


def test_unknown_risk_tier_defaults_to_strictest_verification():
    # Defensive: if a tier ever slips through, it must get the strictest path.
    assert IR.required_verification("nonsense") == IR.MIN_VERIFICATION[IR.RISK_HIGH]


# ── Round-trip ───────────────────────────────────────────────────────────────


def test_ir_roundtrips_through_dict():
    original = _ir(
        hypothesis="将主体核验移到数据查询之前可减少错误实体报告",
        changes=[{"path": "steps.identity_check.order", "before": 3, "after": 1}],
        constraints=["ontology:enterprise_identity"],
    )
    restored = IR.EvolutionIR.from_dict(original.to_dict())
    assert restored.target_asset_id == original.target_asset_id
    assert restored.operation == original.operation
    assert restored.rollback.version_id == original.rollback.version_id
    assert restored.change_checksum() == original.change_checksum()
