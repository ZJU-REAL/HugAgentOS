"""Automatic cycle triggering.

The point of these is the *negative* cases: the trigger must decline far more
often than it fires, and every decline must carry a reason an operator can read.
"""

from datetime import datetime, timedelta, timezone

import pytest

from core.evolution import trigger as T


@pytest.fixture(autouse=True)
def _clean():
    T.reset_for_tests()
    yield
    T.reset_for_tests()


def _now():
    return datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def test_insufficient_new_evidence_declines(monkeypatch):
    """Below MIN_SUPPORT new episodes a cycle provably finds nothing."""
    monkeypatch.setattr(T, "_episode_count", lambda: 3)
    due, reason = T.should_trigger(now=_now())
    assert due is False
    assert "insufficient_new_evidence" in reason


def test_enough_new_evidence_fires(monkeypatch):
    monkeypatch.setattr(T, "_episode_count", lambda: 12)
    due, reason = T.should_trigger(now=_now())
    assert due is True and "12_new_episodes" in reason


def test_debounce_blocks_a_burst(monkeypatch):
    """A burst of conversations must not queue a cycle per turn."""
    monkeypatch.setattr(T, "_episode_count", lambda: 50)
    T._mark_started(50, now=_now())
    T._mark_finished()
    due, reason = T.should_trigger(now=_now() + timedelta(seconds=5))
    assert due is False and reason.startswith("debounce")


def test_debounce_expires(monkeypatch):
    monkeypatch.setattr(T, "_episode_count", lambda: 100)
    T._mark_started(50, now=_now())
    T._mark_finished()
    later = _now() + timedelta(seconds=T.DEBOUNCE_SECONDS + 1)
    due, _ = T.should_trigger(now=later)
    assert due is True


def test_only_new_episodes_count(monkeypatch):
    """After a cycle, the same corpus must not re-trigger — otherwise the
    system would loop on evidence it has already mined."""
    monkeypatch.setattr(T, "_episode_count", lambda: 50)
    T._mark_started(50, now=_now())
    T._mark_finished()
    later = _now() + timedelta(seconds=T.DEBOUNCE_SECONDS + 1)
    due, reason = T.should_trigger(now=later)
    assert due is False and "insufficient_new_evidence:0" in reason


def test_a_running_cycle_blocks_another(monkeypatch):
    monkeypatch.setattr(T, "_episode_count", lambda: 100)
    T._mark_started(0)
    try:
        due, reason = T.should_trigger(now=_now())
        assert due is False and reason == "cycle_already_running"
    finally:
        T._mark_finished()


def test_disabled_declines(monkeypatch):
    monkeypatch.setattr(T, "AUTO_TRIGGER_ENABLED", False)
    monkeypatch.setattr(T, "_episode_count", lambda: 999)
    due, reason = T.should_trigger(now=_now())
    assert due is False and reason == "auto_trigger_disabled"


def test_a_database_failure_declines_rather_than_raising(monkeypatch):
    """The trigger runs after the user has their answer; it must stay silent."""
    monkeypatch.setattr(
        T, "_episode_count", lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    )
    due, reason = T.should_trigger(now=_now())
    assert due is False and reason == "count_unavailable"


def test_run_cycle_returns_none_when_not_due(monkeypatch):
    monkeypatch.setattr(T, "_episode_count", lambda: 1)
    assert T.run_cycle_if_due() is None


def test_cycle_failure_is_swallowed_and_releases_the_lock(monkeypatch):
    monkeypatch.setattr(T, "_episode_count", lambda: 50)
    import core.evolution.loop as L

    monkeypatch.setattr(
        L, "run_evolution_cycle", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert T.run_cycle_if_due() is None
    # The in-flight flag must not stay stuck, or nothing would ever run again.
    assert T._running is False


def test_status_explains_why_it_has_not_run(monkeypatch):
    monkeypatch.setattr(T, "_episode_count", lambda: 2)
    state = T.status()
    assert state["due"] is False
    # Silence is the wrong answer to "why hasn't it run?".
    assert "insufficient_new_evidence" in state["reason"]
    assert state["min_new_episodes"] == T.MIN_NEW_EPISODES
