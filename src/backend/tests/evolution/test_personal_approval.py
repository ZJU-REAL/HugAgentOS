"""Personal-scope candidate approval.

The design point under test: a capability distilled from someone's own
conversations is theirs to accept, and accepting it must not change what anyone
else's agent does. Capabilities learned mainly from other people's evidence stay
with an administrator, because one user approving for the fleet is a governance
hole regardless of how convenient it is.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models.evolution import EvolutionCandidate, EvolutionEpisode
from core.evolution import user_settings as US


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        engine, tables=[EvolutionEpisode.__table__, EvolutionCandidate.__table__]
    )
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(US, "SessionLocal", factory)
    session = factory()
    yield session
    session.close()


def _episode(db, episode_id, user):
    db.add(
        EvolutionEpisode(
            episode_id=episode_id,
            message_id=f"m-{episode_id}",
            user_id=user,
            privacy_class="tenant",
        )
    )


def _candidate(
    db, candidate_id, evidence, status="draft", risk_tier="low", **overrides
):
    """A candidate a user could genuinely accept for themselves.

    ``risk_tier`` defaults to low because that is the only tier a personal
    activation can carry out from a draft: anything higher owes shadow and
    canary, which one person clicking a button cannot provide. Listing those
    anyway is what put rows in the queue whose button could only ever return
    "force 只允许用于 low 风险候选".
    """
    db.add(
        EvolutionCandidate(
            candidate_id=candidate_id,
            target_kind=overrides.pop("target_kind", "skill"),
            target_asset_id=f"auto-{candidate_id}",
            operation=overrides.pop("operation", "new"),
            ir=overrides.pop("ir", {"changes": [{"tool_sequence": ["a", "b"]}]}),
            change_checksum=candidate_id,
            evidence_refs=list(evidence),
            hypothesis="重复出现的工具子序列",
            status=status,
            risk_tier=risk_tier,
            **overrides,
        )
    )


# ── Scoping ──────────────────────────────────────────────────────────────────


def test_a_candidate_from_my_own_evidence_is_mine_to_approve(db):
    for i in range(5):
        _episode(db, f"e{i}", "alice")
    _candidate(db, "c1", [f"e{i}" for i in range(5)])
    db.commit()

    pending = US.pending_for_user("alice")
    assert [c["candidate_id"] for c in pending] == ["c1"]
    assert pending[0]["own_share"] == 1.0


def test_a_candidate_mostly_from_others_is_not_mine_to_approve(db):
    _episode(db, "mine", "alice")
    for i in range(9):
        _episode(db, f"theirs{i}", "bob")
    _candidate(db, "c1", ["mine"] + [f"theirs{i}" for i in range(9)])
    db.commit()

    # 10% of the evidence is Alice's; approving it would activate a capability
    # built from Bob's work.
    assert US.pending_for_user("alice") == []


def test_the_threshold_is_explicit_not_incidental(db):
    for i in range(6):
        _episode(db, f"mine{i}", "alice")
    for i in range(4):
        _episode(db, f"theirs{i}", "bob")
    _candidate(
        db, "c1", [f"mine{i}" for i in range(6)] + [f"theirs{i}" for i in range(4)]
    )
    db.commit()

    pending = US.pending_for_user("alice")
    assert pending and pending[0]["own_share"] == pytest.approx(0.6)
    assert US.OWN_EVIDENCE_THRESHOLD == 0.6


def test_another_users_candidate_never_appears(db):
    for i in range(5):
        _episode(db, f"e{i}", "bob")
    _candidate(db, "c1", [f"e{i}" for i in range(5)])
    db.commit()
    assert US.pending_for_user("alice") == []


def test_already_released_candidates_are_not_pending(db):
    for i in range(5):
        _episode(db, f"e{i}", "alice")
    _candidate(db, "c1", [f"e{i}" for i in range(5)], status="active")
    db.commit()
    assert US.pending_for_user("alice") == []


def test_a_user_with_no_episodes_sees_nothing(db):
    _candidate(db, "c1", ["someone-else"])
    db.commit()
    assert US.pending_for_user("nobody") == []


def test_candidates_without_evidence_are_skipped(db):
    _episode(db, "e0", "alice")
    _candidate(db, "c1", [])
    db.commit()
    assert US.pending_for_user("alice") == []


# ── Approval enforcement ─────────────────────────────────────────────────────


def test_approving_someone_elses_candidate_is_refused(db):
    _episode(db, "mine", "alice")
    for i in range(9):
        _episode(db, f"theirs{i}", "bob")
    _candidate(db, "c1", ["mine"] + [f"theirs{i}" for i in range(9)])
    db.commit()

    with pytest.raises(PermissionError) as exc:
        US.approve_for_user("c1", "alice")
    # The refusal explains the scope rule rather than just denying.
    assert "管理员" in str(exc.value)


def test_the_pending_list_is_the_authorisation_source(db, monkeypatch):
    """Approval re-derives eligibility rather than trusting the caller's id."""
    for i in range(5):
        _episode(db, f"e{i}", "alice")
    _candidate(db, "c1", [f"e{i}" for i in range(5)])
    db.commit()

    called = {}

    def fake_materialise(candidate_id, *, approver, force=False, owner_user_id=None):
        called.update(
            candidate_id=candidate_id, approver=approver, owner=owner_user_id
        )
        return {"skill_id": "evo-x", "tools": ["a", "b"]}

    import core.evolution.activation as A

    monkeypatch.setattr(A, "materialise_skill_candidate", fake_materialise)

    result = US.approve_for_user("c1", "alice")
    # Installed privately, so no other user's agent changes.
    assert called["owner"] == "alice"
    assert called["approver"] == "user:alice"
    assert result["scope"] == "personal"


def test_the_tool_sequence_is_surfaced_so_approval_is_informed(db):
    for i in range(5):
        _episode(db, f"e{i}", "alice")
    _candidate(db, "c1", [f"e{i}" for i in range(5)])
    db.commit()
    pending = US.pending_for_user("alice")
    # Approving a capability without seeing what it will do is a habit, not a
    # decision.
    assert pending[0]["tool_sequence"] == ["a", "b"]


# ── The queue lists only what it can carry out ───────────────────────────────
#
# Every case below used to appear with a 「为我启用」 button whose sole possible
# outcome was an error toast. A queue that offers a decision it cannot execute
# is worse than an empty one: it teaches the reader the feature does nothing.


def test_a_memory_candidate_is_offered_as_a_memory_change_not_an_install(db):
    for i in range(3):
        _episode(db, f"e{i}", "alice")
    _candidate(
        db, "m1", [f"e{i}" for i in range(3)],
        target_kind="memory", operation="reweight",
        ir={"changes": [{"user_id": "alice", "operations": [
            {"operation": "reweight", "memory_ref": "ref-1",
             "after": {"weight": 1.5}}]}]},
    )
    db.commit()

    row = US.pending_for_user("alice")[0]
    assert row["action"] == "apply_memory"
    # Not "为我启用": nothing is being installed, a retrieval weight is changing.
    assert row["action_label"] != "为我启用"
    assert row["change"]["type"] == "memory_ops"


def test_a_memory_operation_with_no_executor_is_not_offered(db):
    # `deprecate` is proposable in the IR but `apply_memory_ops` refuses it with
    # `no_executor_for_operation`, so it must not reach the queue.
    for i in range(3):
        _episode(db, f"e{i}", "alice")
    _candidate(
        db, "m2", [f"e{i}" for i in range(3)],
        target_kind="memory", operation="deprecate",
    )
    db.commit()

    assert US.pending_for_user("alice") == []


def test_a_medium_risk_draft_skill_is_not_personally_approvable(db):
    for i in range(3):
        _episode(db, f"e{i}", "alice")
    _candidate(db, "s2", [f"e{i}" for i in range(3)], risk_tier="medium")
    db.commit()

    assert US.pending_for_user("alice") == []


def test_a_replayed_medium_risk_skill_is_personally_approvable(db):
    # Once replay has passed, a personal activation is the one case that may go
    # straight live: its blast radius is a single consenting user.
    for i in range(3):
        _episode(db, f"e{i}", "alice")
    _candidate(
        db, "s3", [f"e{i}" for i in range(3)],
        status="replay_passed", risk_tier="medium",
    )
    db.commit()

    assert [c["candidate_id"] for c in US.pending_for_user("alice")] == ["s3"]


def test_a_retirement_is_never_offered_as_something_to_enable(db):
    for i in range(3):
        _episode(db, f"e{i}", "alice")
    _candidate(db, "s4", [f"e{i}" for i in range(3)], operation="deprecate")
    db.commit()

    assert US.pending_for_user("alice") == []


def test_an_orchestration_profile_is_not_a_personal_decision(db):
    # A profile governs how every request of a task type is assembled, for
    # everyone. There is no per-user version of it to accept.
    for i in range(3):
        _episode(db, f"e{i}", "alice")
    _candidate(
        db, "p1", [f"e{i}" for i in range(3)],
        target_kind="agent_profile", operation="patch",
    )
    db.commit()

    assert US.pending_for_user("alice") == []


def test_the_skill_body_is_shown_so_approval_is_a_decision(db):
    # Approving a distilled skill without being able to read it is a guess. The
    # text was always in the IR; it simply was never sent to the page.
    for i in range(3):
        _episode(db, f"e{i}", "alice")
    _candidate(
        db, "s5", [f"e{i}" for i in range(3)],
        ir={"changes": [{"document": {
            "display_name": "产业链梳理",
            "description": "梳理上下游并逐环节找代表企业",
            "allowed_tools": ["internet_search"],
            "content": "## 不适用范围\n单家企业尽调不用本技能\n\n## 步骤\n1. 先拉环节划分",
        }}]},
    )
    db.commit()

    change = US.pending_for_user("alice")[0]["change"]
    assert change["type"] == "skill_document"
    assert "## 步骤" in change["content"]
    assert change["allowed_tools"] == ["internet_search"]
