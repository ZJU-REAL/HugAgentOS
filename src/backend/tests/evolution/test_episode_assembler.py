"""GCE ticket 04 — trace events and Episode assembly.

Load-bearing properties: one run yields exactly one Episode however often
assembly retries, evidence collection can never fail the turn, and an Episode
without a full asset snapshot is barred from replay.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models.evolution import EvolutionEpisode, EvolutionTraceEvent
from core.evolution import events as EV
from core.evolution import trace_assembler as TA
from core.evolution import trace_store as TS
from core.evolution.contract import ASSET_SKILL, AssetRef, build_bundle


@pytest.fixture
def db_factory(monkeypatch):
    """In-memory database wired into both the store and the assembler."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        engine, tables=[EvolutionEpisode.__table__, EvolutionTraceEvent.__table__]
    )
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(TS, "SessionLocal", factory)
    monkeypatch.setattr(TA, "SessionLocal", factory)
    # The assembler counts rows in the pre-existing log tables; they are absent
    # here, and degrading to zero rather than raising is the documented behaviour.
    monkeypatch.setattr(TA, "_count_existing_logs", lambda db, mid: {})
    return factory


# ── TraceSink ────────────────────────────────────────────────────────────────


def test_sink_assigns_monotonic_sequence():
    sink = EV.TraceSink(run_id="r1", message_id="m1")
    sink.append(EV.EV_RUN_STARTED)
    sink.append(EV.EV_TOOL_CALLED, {"tool": "search"})
    sink.append(EV.EV_RUN_FINISHED)
    assert [e.seq for e in sink.events] == [1, 2, 3]


def test_sink_never_raises_on_unserializable_payload():
    sink = EV.TraceSink(message_id="m1")

    class Unserializable:
        def __repr__(self):
            raise RuntimeError("nope")

    # A bad payload must degrade to a marker, not blow up the response path.
    event = sink.append(EV.EV_TOOL_CALLED, {"bad": Unserializable()})
    assert event is not None


def test_oversized_payload_is_replaced_by_a_reference():
    sink = EV.TraceSink(message_id="m1")
    event = sink.append(EV.EV_TOOL_CALLED, {"body": "x" * 20000})
    # Result bodies live in their own tables; the event table stays queryable.
    assert event.payload.get("_truncated") is True
    assert event.payload_ref.startswith("oversized:")
    assert "x" * 100 not in str(event.payload)


def test_flush_is_idempotent(db_factory):
    sink = EV.TraceSink(run_id="r1", message_id="m1")
    sink.append(EV.EV_RUN_STARTED)
    sink.append(EV.EV_RUN_FINISHED)

    assert sink.flush() == 2
    # A retried flush must be a silent no-op, not a duplicate write.
    assert sink.flush() == 0
    assert len(TS.load_events("m1")) == 2


def test_persist_ignores_duplicate_sequence_numbers(db_factory):
    first = EV.TraceSink(message_id="m1")
    first.append(EV.EV_RUN_STARTED)
    TS.persist_events(first.events)

    replay = EV.TraceSink(message_id="m1")
    replay.append(EV.EV_RUN_STARTED)  # same (message_id, seq)
    assert TS.persist_events(replay.events) == 0
    assert len(TS.load_events("m1")) == 1


def test_flush_failure_is_swallowed(monkeypatch):
    sink = EV.TraceSink(message_id="m1")
    sink.append(EV.EV_RUN_STARTED)
    monkeypatch.setattr(
        TS, "persist_events", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down"))
    )
    # Evidence is worth having, but never at the cost of the user's answer.
    assert sink.flush() == 0


def test_events_load_in_emission_order(db_factory):
    sink = EV.TraceSink(message_id="m1")
    for i in range(5):
        sink.append(EV.EV_TOOL_CALLED, {"i": i})
    sink.flush()
    assert [e.payload["i"] for e in TS.load_events("m1")] == [0, 1, 2, 3, 4]


# ── Episode assembly ─────────────────────────────────────────────────────────


def _bundle():
    return build_bundle([AssetRef(kind=ASSET_SKILL, asset_id="s1", version="v1")])


def test_assembly_creates_one_episode(db_factory):
    episode_id = TA.assemble_episode(
        message_id="m1", run_id="r1", objective="分析产业链风险", bundle=_bundle()
    )
    assert episode_id
    with db_factory() as db:
        assert db.query(EvolutionEpisode).count() == 1


def test_assembly_is_idempotent(db_factory):
    first = TA.assemble_episode(message_id="m1", run_id="r1", bundle=_bundle())
    second = TA.assemble_episode(message_id="m1", run_id="r1", bundle=_bundle())
    # One run, one Episode — no matter how many times assembly is retried.
    assert first == second
    with db_factory() as db:
        assert db.query(EvolutionEpisode).count() == 1


def test_assembly_backfills_episode_id_onto_earlier_events(db_factory):
    sink = EV.TraceSink(run_id="r1", message_id="m1")
    sink.append(EV.EV_RUN_STARTED)
    sink.flush()

    episode_id = TA.assemble_episode(message_id="m1", run_id="r1", bundle=_bundle())
    assert all(e.episode_id == episode_id for e in TS.load_events("m1"))


def test_assembly_never_raises_on_database_failure(monkeypatch):
    monkeypatch.setattr(
        TA, "SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    )
    assert TA.assemble_episode(message_id="m1", run_id="r1") is None


def test_assembly_requires_a_message_id():
    assert TA.assemble_episode(message_id="") is None


def test_objective_preview_is_sanitized_and_bounded(db_factory):
    TA.assemble_episode(message_id="m1", objective="x" * 5000, bundle=_bundle())
    with db_factory() as db:
        episode = db.query(EvolutionEpisode).first()
        assert len(episode.objective_preview) <= 400


def test_same_objective_hashes_equally_across_formatting(db_factory):
    TA.assemble_episode(message_id="m1", objective="分析  产业链\n风险", bundle=_bundle())
    TA.assemble_episode(message_id="m2", objective="分析 产业链 风险", bundle=_bundle())
    with db_factory() as db:
        hashes = {e.objective_hash for e in db.query(EvolutionEpisode).all()}
        assert len(hashes) == 1


# ── Replay eligibility (the ticket-08 boundary) ──────────────────────────────


def test_fully_bound_episode_is_replay_eligible(db_factory):
    TA.assemble_episode(message_id="m1", bundle=_bundle())
    with db_factory() as db:
        episode = db.query(EvolutionEpisode).first()
        assert TA.is_replay_eligible(episode) is True
        assert TA.replay_rejection_reason(episode) == ""


def test_backfilled_episode_is_barred_from_replay(db_factory):
    TA.assemble_episode(message_id="m1", bundle=_bundle(), backfilled=True)
    with db_factory() as db:
        episode = db.query(EvolutionEpisode).first()
        # Replay's premise is "everything else frozen"; for a backfilled episode
        # we do not know what everything else was.
        assert TA.is_replay_eligible(episode) is False
        assert "backfilled" in TA.replay_rejection_reason(episode)


def test_partial_bundle_is_barred_from_replay(db_factory):
    partial = build_bundle(
        [AssetRef(kind=ASSET_SKILL, asset_id="s1", version="v1")], partial=True
    )
    TA.assemble_episode(message_id="m1", bundle=partial)
    with db_factory() as db:
        episode = db.query(EvolutionEpisode).first()
        assert TA.is_replay_eligible(episode) is False
        assert TA.replay_rejection_reason(episode) == "asset_bundle_incomplete"


def test_episode_without_any_bundle_is_barred_from_replay(db_factory):
    TA.assemble_episode(message_id="m1", bundle=None)
    with db_factory() as db:
        episode = db.query(EvolutionEpisode).first()
        assert episode.bundle_partial is True
        assert TA.is_replay_eligible(episode) is False


def test_missing_episode_is_not_replay_eligible():
    assert TA.is_replay_eligible(None) is False
    assert TA.replay_rejection_reason(None) == "episode_not_found"
