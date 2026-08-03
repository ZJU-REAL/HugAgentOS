"""Release exposure and the ramp — the gate that was missing at runtime.

The state machine modelled ``shadow → canary → active`` and the IR declared a
``scope``, but neither reached the runtime: materialisation wrote ``active`` at
100% traffic as a global public skill, so a ``risk_tier=high`` capability the
system wrote about itself went to every user the moment it was activated.

These tests pin the three properties that fixes that: what each stage exposes,
that the ladder is climbed one rung at a time, and that the tier's declared
verification is actually owed.
"""

import pytest

from core.evolution import release as R
from core.evolution.activation import _entry_stage, ActivationError
from core.evolution.exposure import is_evolved_skill_id, visible_evolved_skill_ids


class _Candidate:
    """Just the fields the entry-stage decision reads."""

    def __init__(self, risk_tier="high", status=R.REPLAY_PASSED, proposer="promotion_chain"):
        self.risk_tier = risk_tier
        self.status = status
        self.proposer = proposer


class _Release:
    def __init__(self, stage, traffic=0, scope=None, release_id="rel_test", asset="evo-x"):
        self.release_id = release_id
        self.target_asset_id = asset
        self.target_kind = "skill"
        self.stage = stage
        self.traffic_percent = traffic
        self.scope_filter = scope or {}
        self.created_at = None


class _FakeDB:
    """Stands in for the release query; keeps these tests free of a database."""

    def __init__(self, releases):
        self._releases = releases

    def query(self, *_args, **_kwargs):
        return self

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._releases


# ── Entry stage per risk tier ────────────────────────────────────────────────


def test_a_high_risk_new_skill_enters_shadow_not_production():
    """The old behaviour skipped two of the four stages its own IR demanded."""
    stage, traffic = _entry_stage(
        _Candidate(risk_tier="high"), approver="ops", force=False, owner_user_id=None
    )
    assert stage == R.SHADOW
    assert traffic == 0


def test_a_low_risk_change_may_go_live_directly():
    """Its declared verification is replay alone, which it has."""
    stage, traffic = _entry_stage(
        _Candidate(risk_tier="low"), approver="ops", force=False, owner_user_id=None
    )
    assert (stage, traffic) == (R.ACTIVE, 100)


def test_a_personal_activation_goes_live_scoped_to_one_user():
    """The ramp bounds blast radius; one consenting user already is bounded."""
    stage, traffic = _entry_stage(
        _Candidate(risk_tier="high"), approver="ops", force=False, owner_user_id="u1"
    )
    assert stage == R.ACTIVE
    # Recorded as scoped rather than as full traffic, so the release board does
    # not report a one-user change as a fleet-wide one.
    assert traffic == 0


def test_replay_is_still_required():
    with pytest.raises(ActivationError) as exc:
        _entry_stage(
            _Candidate(status=R.DRAFT), approver="ops", force=False, owner_user_id=None
        )
    assert exc.value.code == "verification_incomplete"


def test_force_cannot_waive_replay_for_a_tier_that_also_owes_shadow_and_canary():
    """Waiving it there is not a shortcut — it is the whole gate."""
    with pytest.raises(ActivationError) as exc:
        _entry_stage(
            _Candidate(risk_tier="high", status=R.DRAFT),
            approver="ops",
            force=True,
            owner_user_id=None,
        )
    assert exc.value.code == "force_forbidden_for_risk"


def test_force_is_allowed_for_the_tier_whose_only_requirement_is_replay():
    stage, _ = _entry_stage(
        _Candidate(risk_tier="low", status=R.DRAFT),
        approver="ops",
        force=True,
        owner_user_id=None,
    )
    assert stage == R.ACTIVE


# ── Exposure per stage ───────────────────────────────────────────────────────


def test_shadow_reaches_nobody():
    """A shadow stage that leaked to users would just be an unannounced canary."""
    db = _FakeDB([_Release(R.SHADOW, 0, asset="evo-a")])
    assert visible_evolved_skill_ids(db, ["evo-a"], user_id="u1") == set()


def test_active_reaches_everyone_in_scope():
    db = _FakeDB([_Release(R.ACTIVE, 100, asset="evo-a")])
    assert visible_evolved_skill_ids(db, ["evo-a"], user_id="u1") == {"evo-a"}


def test_canary_reaches_a_stable_fraction():
    db = _FakeDB([_Release(R.CANARY, 5, release_id="rel_c", asset="evo-a")])
    subjects = [f"user-{i}" for i in range(400)]
    hit = [s for s in subjects if visible_evolved_skill_ids(db, ["evo-a"], user_id=s)]
    # A 5% bucket over 400 subjects: loose bounds, since the point is that it is
    # a small minority rather than an exact rate.
    assert 0 < len(hit) < 80
    # Stable: the same subject gets the same answer every time, so a user does
    # not flip between versions mid-ramp.
    for subject in hit[:5]:
        assert visible_evolved_skill_ids(db, ["evo-a"], user_id=subject) == {"evo-a"}


def test_a_rolled_back_release_stops_exposing_its_skill():
    db = _FakeDB([_Release(R.ROLLED_BACK, 0, asset="evo-a")])
    assert visible_evolved_skill_ids(db, ["evo-a"], user_id="u1") == set()


def test_an_evolved_skill_with_no_release_is_withheld():
    """Fail closed: the unsafe direction is loading an unvetted self-authored
    capability, so absence of evidence is not evidence of approval."""
    assert visible_evolved_skill_ids(_FakeDB([]), ["evo-a"], user_id="u1") == set()


def test_hand_authored_skills_are_never_filtered():
    from core.evolution.exposure import filter_skill_ids

    ids = ["word-editing", "ppt-design"]
    assert filter_skill_ids(ids, user_id="u1") == ids
    assert not any(is_evolved_skill_id(sid) for sid in ids)


def test_owner_scope_keeps_a_personal_activation_personal():
    db = _FakeDB(
        [_Release(R.ACTIVE, 0, scope={"owner_user_id": "u1"}, asset="evo-a")]
    )
    assert visible_evolved_skill_ids(db, ["evo-a"], user_id="u1") == {"evo-a"}
    assert visible_evolved_skill_ids(db, ["evo-a"], user_id="u2") == set()


def test_tenant_scope_does_not_leak_across_tenants():
    db = _FakeDB([_Release(R.ACTIVE, 100, scope={"tenant_id": "acme"}, asset="evo-a")])
    assert visible_evolved_skill_ids(db, ["evo-a"], user_id="u1", tenant_id="acme") == {"evo-a"}
    assert visible_evolved_skill_ids(db, ["evo-a"], user_id="u1", tenant_id="other") == set()


# ── The ladder ───────────────────────────────────────────────────────────────


def test_the_ramp_steps_are_small_first():
    assert R.CANARY_STEPS[0] < R.CANARY_STEPS[-1] == 100
    assert R.next_traffic(0) == R.CANARY_STEPS[0]
    assert R.next_traffic(5) == 25
    assert R.next_traffic(100) == 100


def test_going_live_needs_every_stage_the_tier_declares():
    ok, reason = R.can_promote(
        current_stage=R.CANARY,
        target_stage=R.ACTIVE,
        risk_tier="high",
        completed_verification=["replay"],  # shadow and canary missing
        proposer="promotion_chain",
        approver="ops",
    )
    assert ok is False
    assert "missing_verification" in reason

    ok, _ = R.can_promote(
        current_stage=R.CANARY,
        target_stage=R.ACTIVE,
        risk_tier="high",
        completed_verification=["replay", "shadow", "canary", "human_approval"],
        proposer="promotion_chain",
        approver="ops",
    )
    assert ok is True


def test_the_proposer_still_cannot_sign_off_its_own_release():
    ok, reason = R.can_promote(
        current_stage=R.CANARY,
        target_stage=R.ACTIVE,
        risk_tier="low",
        completed_verification=["replay", "shadow", "canary", "human_approval"],
        proposer="promotion_chain",
        approver="promotion_chain",
    )
    assert ok is False
    assert reason == "self_approval_forbidden"
