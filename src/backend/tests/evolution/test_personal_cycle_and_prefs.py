"""Personal evolution scope, edition gating, and fine-grained preferences."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models.evolution import EvolutionEpisode, EvolutionTraceEvent
from core.evolution import personal as PE
from core.evolution import prefs as PF


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        engine, tables=[EvolutionEpisode.__table__, EvolutionTraceEvent.__table__]
    )
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(PE, "SessionLocal", factory)
    session = factory()
    yield session
    session.close()


def _episode(db, episode_id, user, tools):
    db.add(
        EvolutionEpisode(
            episode_id=episode_id,
            message_id=f"m-{episode_id}",
            user_id=user,
            privacy_class="tenant",
            quality_score=0.9,
            outcome={"verdict": "success"},
        )
    )
    for i, tool in enumerate(tools, start=1):
        db.add(
            EvolutionTraceEvent(
                event_id=f"{episode_id}-{i}",
                episode_id=episode_id,
                message_id=f"m-{episode_id}",
                seq=i,
                event_type="tool.called",
                payload={"tool_name": tool},
            )
        )


# ── Scope isolation ──────────────────────────────────────────────────────────


def test_a_personal_cycle_cannot_see_another_users_episodes(db):
    for i in range(4):
        _episode(db, f"mine{i}", "alice", ["a", "b"])
    for i in range(9):
        _episode(db, f"theirs{i}", "bob", ["x", "y"])
    db.commit()

    views = PE.load_personal_episodes("alice")
    assert len(views) == 4
    # Filtering happens in the query, so leakage is structurally impossible
    # rather than merely unlikely.
    assert all(v["tool_sequence"] == ["a", "b"] for v in views)


def test_an_unknown_user_gets_nothing(db):
    _episode(db, "e0", "alice", ["a"])
    db.commit()
    assert PE.load_personal_episodes("nobody") == []
    assert PE.load_personal_episodes("") == []


def test_personal_support_threshold_is_lower_than_fleet_wide():
    from core.evolution.promotion import MIN_SUPPORT

    # One person's blast radius is one person; demanding fleet-scale evidence
    # would mean personal evolution never fires.
    assert PE.PERSONAL_MIN_SUPPORT < MIN_SUPPORT


def test_a_disabled_user_produces_no_candidates(db, monkeypatch):
    for i in range(5):
        _episode(db, f"e{i}", "alice", ["a", "b"])
    db.commit()
    monkeypatch.setattr(PE, "resolve_prefs", lambda uid: {"enabled": False}, raising=False)
    import core.evolution.prefs as prefs_mod

    monkeypatch.setattr(prefs_mod, "resolve_prefs", lambda uid: {"enabled": False})
    report = PE.run_personal_cycle("alice")
    assert report.get("skipped") == "evolution_disabled_for_user"


# ── Edition gating ───────────────────────────────────────────────────────────


def test_cross_user_mining_is_skipped_without_the_entitlement(monkeypatch):
    import core.evolution.loop as L

    monkeypatch.setattr(L, "cross_user_mining_available", lambda: False)
    report = L.run_evolution_cycle(limit=10)
    assert report["skipped"] == "cross_user_mining_requires_enterprise"
    assert report["candidates"] == []


def test_a_missing_licensing_module_reads_as_community(monkeypatch):
    """The community tree excludes edition_ee entirely, so an ImportError is the
    expected signal — not a fault to be logged and retried."""
    import builtins
    import sys

    import core.evolution.loop as L

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("edition_ee"):
            raise ImportError("no edition_ee in community tree")
        return real_import(name, *args, **kwargs)

    # Evict only the two licensing modules this test blocks.
    # `importlib.import_module` returns straight from `sys.modules` without ever
    # calling `__import__`, so with a warm cache the assertion below was vacuous:
    # it passed alone and failed once an earlier test in the run had imported
    # them. Evicting the whole `edition_ee` package instead would force
    # `edition_ee.db.models` to re-import and re-register its tables on the
    # shared SQLAlchemy metadata, breaking unrelated tests later in the run.
    for name in ("edition_ee.licensing.features", "edition_ee.licensing.manager"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert L.cross_user_mining_available() is False


# ── Preferences ──────────────────────────────────────────────────────────────


def test_ontology_defaults_to_none_so_unconfigured_deployments_work():
    """Assuming a domain pack exists is what breaks deployments that have none."""
    assert PF.normalise(None)["ontology_mode"] == PF.ONTOLOGY_NONE


def test_specific_without_a_pack_id_degrades_to_none():
    prefs = PF.normalise({"ontology_mode": "specific", "ontology_pack_id": ""})
    # Otherwise every candidate would be rejected for an unresolvable reference.
    assert prefs["ontology_mode"] == PF.ONTOLOGY_NONE


def test_a_stale_pack_id_is_cleared_when_the_mode_changes():
    prefs = PF.normalise({"ontology_mode": "any", "ontology_pack_id": "gone"})
    assert prefs["ontology_pack_id"] == ""


def test_an_unknown_mode_falls_back_rather_than_raising():
    assert PF.normalise({"ontology_mode": "whatever"})["ontology_mode"] == PF.ONTOLOGY_NONE


def test_specific_with_a_pack_id_is_honoured():
    prefs = PF.normalise({"ontology_mode": "specific", "ontology_pack_id": "enterprise"})
    assert prefs["ontology_mode"] == PF.ONTOLOGY_SPECIFIC
    assert prefs["ontology_pack_id"] == "enterprise"


def test_mechanisms_are_filtered_and_never_empty():
    assert PF.normalise({"mechanisms": ["skill", "bogus"]})["mechanisms"] == ["skill"]
    # An empty selection would silently disable evolution without saying so.
    assert PF.normalise({"mechanisms": []})["mechanisms"] == list(PF.ALL_MECHANISMS)


def test_min_support_is_clamped_to_a_sane_range():
    assert PF.normalise({"min_support": 0})["min_support"] == 2
    assert PF.normalise({"min_support": 999})["min_support"] == 20
    assert PF.normalise({"min_support": "x"})["min_support"] == 3


def test_no_ontology_configured_skips_the_concept_check():
    """A deployment with no domain pack must still be able to evolve."""
    assert PE._known_concepts({"ontology_mode": PF.ONTOLOGY_NONE}) is None
    assert PE._known_concepts({"ontology_mode": PF.ONTOLOGY_SPECIFIC}) is None
