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


def _candidate(db, candidate_id, evidence, status="draft"):
    db.add(
        EvolutionCandidate(
            candidate_id=candidate_id,
            target_kind="skill",
            target_asset_id=f"auto-{candidate_id}",
            operation="new",
            ir={"changes": [{"tool_sequence": ["a", "b"]}]},
            change_checksum=candidate_id,
            evidence_refs=list(evidence),
            hypothesis="重复出现的工具子序列",
            status=status,
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
