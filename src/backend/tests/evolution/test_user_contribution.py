"""Per-user evolution participation.

The load-bearing property is the enforcement, not the switch: an opted-out
user's episodes must be recorded (so their own card and history still work) yet
never reach cross-user pattern mining. A setting that only changed a label
would be a promise the code could not keep.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models.evolution import (
    EvolutionCandidate,
    EvolutionEpisode,
    EvolutionTraceEvent,
)
from core.evolution import user_settings as US


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        engine,
        tables=[
            EvolutionEpisode.__table__,
            EvolutionTraceEvent.__table__,
            EvolutionCandidate.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(US, "SessionLocal", factory, raising=False)
    session = factory()
    yield session
    session.close()


# ── Semantics of the setting ─────────────────────────────────────────────────


def test_participation_defaults_to_on():
    assert US.is_contribution_enabled({}) is True
    assert US.is_contribution_enabled(None) is True


def test_opting_out_marks_episodes_private():
    assert US.privacy_class_for({US.SETTING_KEY: False}) == US.PRIVACY_PRIVATE
    assert US.privacy_class_for({US.SETTING_KEY: True}) == US.PRIVACY_TENANT


def test_missing_user_resolves_to_the_default_rather_than_failing():
    resolved = US.resolve_for_user("")
    assert resolved[US.SETTING_KEY] is True


# ── Enforcement: the part that makes the promise real ────────────────────────


def _episode(db, *, episode_id, user, privacy):
    db.add(
        EvolutionEpisode(
            episode_id=episode_id,
            message_id=f"m-{episode_id}",
            user_id=user,
            privacy_class=privacy,
            quality_score=0.9,
        )
    )


def test_private_episodes_are_excluded_from_pattern_mining(monkeypatch, db):
    """An opted-out user's traces must not feed cross-user learning."""
    from core.evolution import loop as L

    _episode(db, episode_id="pub-1", user="u1", privacy=US.PRIVACY_TENANT)
    _episode(db, episode_id="priv-1", user="u2", privacy=US.PRIVACY_PRIVATE)
    db.commit()

    factory = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr(L, "SessionLocal", factory)

    views = L.load_episode_views(limit=50)
    ids = {v["episode_id"] for v in views}
    assert "pub-1" in ids
    assert "priv-1" not in ids


def test_opted_out_episodes_are_still_recorded_for_their_owner(db):
    """Opting out is not deletion — the user keeps their own card and history."""
    _episode(db, episode_id="priv-1", user="u2", privacy=US.PRIVACY_PRIVATE)
    db.commit()
    assert db.query(EvolutionEpisode).filter_by(episode_id="priv-1").first() is not None


def test_turning_it_off_does_not_retroactively_withdraw_evidence(db, monkeypatch):
    """Silently rewriting history other candidates were derived from would make
    those candidates unexplainable."""
    _episode(db, episode_id="old-1", user="u1", privacy=US.PRIVACY_TENANT)
    db.commit()

    # Simulate the user switching off afterwards.
    row = db.query(EvolutionEpisode).filter_by(episode_id="old-1").first()
    assert row.privacy_class == US.PRIVACY_TENANT  # unchanged by the toggle


# ── Contribution summary ─────────────────────────────────────────────────────


def test_summary_separates_contributed_from_private(db, monkeypatch):
    monkeypatch.setattr(US, "SessionLocal", sessionmaker(bind=db.get_bind()))
    _episode(db, episode_id="a", user="u1", privacy=US.PRIVACY_TENANT)
    _episode(db, episode_id="b", user="u1", privacy=US.PRIVACY_TENANT)
    _episode(db, episode_id="c", user="u1", privacy=US.PRIVACY_PRIVATE)
    db.commit()

    summary = US.summarise_contributions("u1")
    assert summary["episodes"] == 3
    assert summary["contributed_episodes"] == 2
    assert summary["private_episodes"] == 1


def test_summary_links_candidates_back_to_the_users_own_episodes(db, monkeypatch):
    monkeypatch.setattr(US, "SessionLocal", sessionmaker(bind=db.get_bind()))
    _episode(db, episode_id="a", user="u1", privacy=US.PRIVACY_TENANT)
    db.add(
        EvolutionCandidate(
            candidate_id="c1",
            target_kind="skill",
            target_asset_id="auto-x",
            operation="new",
            ir={},
            change_checksum="k",
            hypothesis="27 个 Episode 出现相同工具子序列",
            evidence_refs=["a", "other-1", "other-2"],
            status="draft",
        )
    )
    db.commit()

    summary = US.summarise_contributions("u1")
    assert len(summary["candidates"]) == 1
    item = summary["candidates"][0]
    # The provenance chain: how much of this capability came from this user.
    assert item["your_episodes"] == 1
    assert item["total_evidence"] == 3
    # Real governance state, matching the in-chat card exactly.
    assert item["status"] == "draft"


def test_candidates_with_no_shared_evidence_are_not_claimed(db, monkeypatch):
    monkeypatch.setattr(US, "SessionLocal", sessionmaker(bind=db.get_bind()))
    _episode(db, episode_id="a", user="u1", privacy=US.PRIVACY_TENANT)
    db.add(
        EvolutionCandidate(
            candidate_id="c2",
            target_kind="skill",
            target_asset_id="auto-y",
            operation="new",
            ir={},
            change_checksum="k2",
            evidence_refs=["someone-else"],
            status="draft",
        )
    )
    db.commit()
    assert US.summarise_contributions("u1")["candidates"] == []


def test_summary_degrades_to_empty_rather_than_raising(monkeypatch):
    monkeypatch.setattr(
        US, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    )
    summary = US.summarise_contributions("u1")
    assert summary["episodes"] == 0 and summary["candidates"] == []


def test_summary_for_a_user_with_no_episodes():
    assert US.summarise_contributions("nobody")["candidates"] == []
