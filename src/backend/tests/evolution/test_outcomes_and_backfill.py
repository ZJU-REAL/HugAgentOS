"""GCE tickets 08 + 09 — historical backfill and multi-signal outcomes."""

import pytest

from core.evolution import outcomes as O


# ── No single judge decides ──────────────────────────────────────────────────


def test_denied_gate_overrides_a_thumbs_up():
    outcome = O.build_outcome(feedback_rating="like", ontology_denied=True)
    # A user's approval cannot make a blocked output a success.
    assert outcome.hard_failed is True
    assert outcome.verdict == "failed"
    assert outcome.quality_score == 0.0


def test_forged_ratings_alone_cannot_carry_a_turn():
    """A rating is cheap to fabricate, so it informs but must not decide."""
    only_rating = O.build_outcome(feedback_rating="like")
    with_evidence = O.build_outcome(feedback_rating="like", artifacts_openable=True)
    # Same rating, but the turn with real evidence is more confidently judged.
    assert only_rating.confidence < with_evidence.confidence
    assert only_rating.confidence < 0.5


def test_deterministic_artifact_check_outweighs_a_like():
    outcome = O.build_outcome(feedback_rating="like", artifacts_openable=False)
    # The file either opens or it does not; no opinion overrides that.
    assert outcome.quality_score is not None and outcome.quality_score < 0.5


def test_triaged_user_report_is_the_strongest_negative():
    outcome = O.build_outcome(
        feedback_rating="like",
        user_report={"disposition": "confirmed", "issue": "#42"},
    )
    assert outcome.verdict in ("failed", "mixed")
    assert outcome.quality_score < 0.6


# ── Graceful degradation ─────────────────────────────────────────────────────


def test_no_signals_yields_unknown_not_a_guess():
    outcome = O.build_outcome()
    assert outcome.verdict == "unknown"
    assert outcome.quality_score is None
    assert outcome.confidence == 0.0


def test_confidence_grows_with_breadth_of_evidence():
    thin = O.build_outcome(feedback_rating="like")
    thick = O.build_outcome(
        feedback_rating="like",
        plan_accepted=True,
        reviewer_verdict="pass",
        artifacts_openable=True,
        tool_call_count=10,
        tool_error_count=0,
    )
    assert thick.confidence > thin.confidence


def test_tool_error_rate_pushes_quality_down():
    clean = O.build_outcome(tool_call_count=10, tool_error_count=0)
    broken = O.build_outcome(tool_call_count=10, tool_error_count=8)
    assert clean.quality_score > broken.quality_score


def test_reviewer_escalation_is_worse_than_revision():
    revise = O.build_outcome(reviewer_verdict="revise")
    escalate = O.build_outcome(reviewer_verdict="escalate")
    assert escalate.quality_score < revise.quality_score


def test_cost_and_latency_are_recorded_without_polluting_quality():
    cheap = O.build_outcome(artifacts_openable=True, cost_usd=0.01, latency_ms=900)
    dear = O.build_outcome(artifacts_openable=True, cost_usd=9.9, latency_ms=90000)
    # Cost is a separate dimension — it must not silently be folded into
    # "quality", or an expensive-but-correct answer looks wrong.
    assert cheap.quality_score == dear.quality_score
    assert dear.cost_usd == 9.9 and dear.latency_ms == 90000


def test_outcome_serialises_every_signal():
    outcome = O.build_outcome(feedback_rating="dislike", tool_call_count=3, tool_error_count=3)
    payload = outcome.to_dict()
    names = {s["name"] for s in payload["signals"]}
    assert O.SIG_FEEDBACK in names and O.SIG_TOOL_ERRORS in names


# ── Late-arriving signals ────────────────────────────────────────────────────


def test_late_signal_revises_a_stored_outcome():
    original = O.build_outcome(tool_call_count=5, tool_error_count=0).to_dict()
    revised = O.merge_late_signal(original, feedback_rating="dislike")
    # Ignoring late feedback would bias the evidence toward turns nobody revisited.
    assert revised["revised"] is True
    assert revised["previous_verdict"] == original["verdict"]


def test_late_signal_preserves_a_prior_hard_failure():
    original = O.build_outcome(ontology_denied=True).to_dict()
    revised = O.merge_late_signal(original, feedback_rating="like")
    # A late thumbs-up must not resurrect a turn the gate blocked.
    assert revised["hard_failed"] is True


# ── Backfill boundary ────────────────────────────────────────────────────────


def test_backfilled_episodes_are_marked_and_barred_from_replay(monkeypatch):
    """The safety property of ticket 08, asserted at the assembler boundary."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core.db.engine import Base
    from core.db.models.evolution import EvolutionEpisode, EvolutionTraceEvent
    from core.evolution import trace_assembler as TA
    from core.evolution import trace_store as TS

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        engine, tables=[EvolutionEpisode.__table__, EvolutionTraceEvent.__table__]
    )
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(TA, "SessionLocal", factory)
    monkeypatch.setattr(TS, "SessionLocal", factory)
    monkeypatch.setattr(TA, "_count_existing_logs", lambda db, mid: {})

    TA.assemble_episode(message_id="hist-1", objective="老任务", bundle=None, backfilled=True)
    with factory() as db:
        episode = db.query(EvolutionEpisode).first()
        assert episode.backfilled is True
        # Replay means "hold everything else fixed" — for a historical run we do
        # not know what everything else was, so it must be refused.
        assert TA.is_replay_eligible(episode) is False
        assert "backfilled" in TA.replay_rejection_reason(episode)


def test_backfill_module_imports_and_exposes_bounded_batching():
    from core.evolution import backfill

    assert backfill.DEFAULT_BATCH_SIZE > 0
    # Cap exists so a large history cannot monopolise the database.
    assert "max_batches" in backfill.backfill_episodes.__code__.co_varnames
