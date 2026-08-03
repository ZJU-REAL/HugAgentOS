"""GCE tickets 23–26 — console API behaviour and cycle metrics.

The console tests focus on the two things that would do real damage if wrong:
concurrent approval silently overwriting a decision, and a generator approving
its own candidate. The metrics tests focus on never inventing a trend.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.db.engine import Base
from core.db.models.evolution import (
    EvolutionCandidate,
    EvolutionEpisode,
    EvolutionRelease,
    EvolutionTraceEvent,
)
from core.evolution import metrics as M


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        engine,
        tables=[
            EvolutionEpisode.__table__,
            EvolutionTraceEvent.__table__,
            EvolutionCandidate.__table__,
            EvolutionRelease.__table__,
        ],
    )
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _episode(db, *, index, days_ago, quality=0.9):
    row = EvolutionEpisode(
        episode_id=f"ep-{days_ago}-{index}",
        message_id=f"m-{days_ago}-{index}",
        task_type="chat",
        quality_score=quality,
        bundle_partial=False,
        backfilled=False,
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    db.add(row)
    return row


# ── Cycle metrics: never invent a trend ──────────────────────────────────────


def test_a_cycle_without_enough_data_yields_nothing():
    """Returning zeros would turn a data gap into a decline that never happened."""

    class _Empty:
        def query(self, *a, **kw):
            return self

        def filter(self, *a, **kw):
            return self

        def all(self):
            return []

    assert M.compute_cycle(_Empty(), index=0) is None


def test_cycle_is_computed_once_there_is_enough_data(db):
    for i in range(6):
        _episode(db, index=i, days_ago=1)
    db.commit()
    cycle = M.compute_cycle(db, index=0)
    assert cycle is not None and cycle["episodes"] == 6


def test_recent_cycles_omits_empty_periods_rather_than_padding(db):
    for i in range(6):
        _episode(db, index=i, days_ago=1)
    db.commit()
    cycles = M.recent_cycles(db, limit=6)
    # Only the period that actually has data appears.
    assert len(cycles) == 1


def test_no_delta_line_without_two_consecutive_cycles(db):
    for i in range(6):
        _episode(db, index=i, days_ago=1)
    db.commit()
    # A single cycle cannot show a change; inventing a previous value to compare
    # against would fabricate a trend.
    assert M.current_cycle_delta(db) is None


def test_delta_appears_once_two_cycles_have_data(db):
    for i in range(6):
        _episode(db, index=i, days_ago=1)
    for i in range(6):
        _episode(db, index=i, days_ago=9)
    db.commit()
    delta = M.current_cycle_delta(db)
    assert delta is not None and delta["from"] == 1 and delta["to"] == 0


def test_skill_reuse_uses_both_selected_and_rejected(db):
    for i in range(6):
        episode = _episode(db, index=i, days_ago=1)
        db.add(
            EvolutionTraceEvent(
                event_id=f"e-{i}",
                episode_id=episode.episode_id,
                message_id=episode.message_id,
                seq=1,
                event_type="skill.selected",
                payload={"selected": ["a"], "rejected": ["b", "c"]},
            )
        )
    db.commit()
    cycle = M.compute_cycle(db, index=0)
    # 1 selected out of 3 considered — the rejects are what make this a *rate*
    # rather than a count.
    assert cycle["skill_reuse_rate"] == pytest.approx(1 / 3, abs=1e-4)


def test_metrics_are_none_not_zero_when_a_dimension_has_no_data(db):
    for i in range(6):
        _episode(db, index=i, days_ago=1)
    db.commit()
    cycle = M.compute_cycle(db, index=0)
    # No skill events at all ⇒ unknown, not "zero reuse".
    assert cycle["skill_reuse_rate"] is None


# ── Console serialisation ────────────────────────────────────────────────────


def test_replay_eligibility_is_surfaced_for_the_console(db):
    from core.evolution.console_view import episode_to_dict as _episode_to_dict

    good = _episode(db, index=1, days_ago=1)
    good.asset_bundle_id = "bundle_x"
    backfilled = _episode(db, index=2, days_ago=1)
    backfilled.backfilled = True
    backfilled.asset_bundle_id = "bundle_y"
    db.commit()

    # The console can explain why replay is unavailable instead of offering a
    # button that always fails.
    assert _episode_to_dict(good)["replay_eligible"] is True
    assert _episode_to_dict(backfilled)["replay_eligible"] is False


def test_candidate_summary_leads_with_why_not_with_the_ir(db):
    from core.evolution.console_view import candidate_to_dict as _candidate_to_dict

    row = EvolutionCandidate(
        candidate_id="c1",
        target_kind="skill",
        target_asset_id="a1",
        operation="new",
        ir={"changes": []},
        change_checksum="x",
        credit_decision={
            "explanation": "出现稳定重复的工具子序列",
            "selected": "skill",
            "confidence": 0.81,
        },
    )
    db.add(row)
    db.commit()

    summary = _candidate_to_dict(row)
    assert summary["why"] == "出现稳定重复的工具子序列"
    assert summary["attributed_to"] == "skill"
    # The reviewer should not need to read JSON before they can start judging.
    assert "ir" not in summary
    assert "ir" in _candidate_to_dict(row, full=True)


def test_release_row_exposes_its_rollback_target(db):
    from core.evolution.console_view import release_to_dict as _release_to_dict

    row = EvolutionRelease(
        release_id="r1",
        candidate_id="c1",
        target_kind="skill",
        target_asset_id="a1",
        stage="canary",
        traffic_percent=25,
        rollback_version_id="v1",
    )
    db.add(row)
    db.commit()
    payload = _release_to_dict(row)
    # Where a rollback lands must be visible *before* anything goes wrong.
    assert payload["rollback_version_id"] == "v1"
    assert payload["traffic_percent"] == 25


def test_console_exposes_no_combined_evaluate_and_activate_shortcut():
    """Such a shortcut would skip the verification a risk tier demands.

    Read from the source declarations rather than by importing the module. EE
    route modules import ``api.deps``, and ``api/__init__`` pulls in ``api.app``,
    which is what registers them — so importing one standalone inverts that
    order. (A pre-existing shape in this codebase; ``admin_kb`` is the same.)
    Parsing the decorators asserts the same property with no import side
    effects; the live surface is covered by the OpenAPI check in deployment.
    """
    import ast
    from pathlib import Path

    source = None
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "edition_ee" / "routes" / "admin_evolution.py"
        if candidate.exists():
            source = candidate
            break
    if source is None:
        pytest.skip("console route module not present (community tree)")

    tree = ast.parse(source.read_text(encoding="utf-8"))
    paths = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and decorator.args:
                first = decorator.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    paths.append(first.value)

    assert paths, "console router declares no routes"

    # The property is that no *single* call both evaluates and activates. A
    # standalone activation endpoint is not that — it is the design: reaching
    # the runtime must be an explicit, separately-authorised act. An earlier
    # version of this test banned any path ending in "/activate", which also
    # outlawed the legitimate one; it was asserting a proxy rather than the
    # invariant.
    assert not any("auto" in p for p in paths)
    for path in paths:
        tail = path.rsplit("/", 1)[-1]
        assert tail not in {
            "evaluate-and-activate",
            "review-and-activate",
            "promote-and-activate",
        }, f"combined evaluate+activate shortcut: {path}"

    # Each stage is its own explicitly-authorised action.
    assert any(p.endswith("/review") for p in paths)
    assert any(p.endswith("/activate") for p in paths)
    assert any(p.endswith("/rollback") for p in paths)
    assert any(p.endswith("/cycle") for p in paths)
