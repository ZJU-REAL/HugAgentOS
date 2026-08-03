"""The card settles on the memories the write pipeline actually persisted.

Two background tasks finish at different times: evidence assembly (fast) and the
memory write pipeline (seconds later, LLM extractors). Settlement waits for the
pipeline, because it is the only party that knows what was written — and,
crucially, *which* entries, since the card offers to edit and delete each one.
"""

import time

import pytest

from core.evolution import settlement_runner as R


@pytest.fixture(autouse=True)
def _clean_runner():
    R.reset_for_tests()
    yield
    R.reset_for_tests()


@pytest.fixture
def persisted(monkeypatch):
    """Capture what settlement persists instead of touching the DB."""
    calls = []
    monkeypatch.setattr(
        "core.evolution.settlement_store.persist_summary",
        lambda message_id, summary: calls.append((message_id, summary)) or True,
    )
    return calls


def _item(handle="m-1", text="先核验主体再取数"):
    return {"layer": "L2", "handle": handle, "text": text, "kind": "procedure"}


def test_nothing_settles_until_the_memory_pipeline_reports(persisted):
    R.register_turn(message_id="m1", episode_id="ep1", expects_memory=True)
    assert persisted == []

    R.report_memory_writes("m1", items=[_item()])
    assert len(persisted) == 1
    summary = persisted[0][1]
    assert summary.state == "settled"
    assert summary.entries[0].handle == "m-1"


def test_a_report_arriving_before_registration_is_not_lost(persisted):
    # The pipeline can finish first on a fast turn; the report is parked until
    # the evidence side registers, rather than settling against nothing.
    R.report_memory_writes("m1", items=[_item(handle="m-9")])
    assert persisted == []

    R.register_turn(message_id="m1", episode_id="ep1", expects_memory=True)
    assert persisted[0][1].entries[0].handle == "m-9"


def test_a_turn_with_memory_disabled_settles_immediately(persisted):
    R.register_turn(message_id="m1", episode_id="ep1", expects_memory=False)
    assert len(persisted) == 1
    assert persisted[0][1].state == "empty"


def test_a_pipeline_that_wrote_nothing_settles_empty_not_pending(persisted):
    # The common case by a wide margin: most turns state no reusable procedure.
    # It must reach a terminal state, or the client polls until it gives up and
    # shows a settlement failure for a turn that simply had nothing.
    R.register_turn(message_id="m1", episode_id="ep1", expects_memory=True)
    R.report_memory_writes("m1", items=[])
    assert persisted[0][1].state == "empty"


def test_settling_happens_once_even_if_both_sides_report_twice(persisted):
    R.register_turn(message_id="m1", episode_id="ep1", expects_memory=True)
    R.report_memory_writes("m1", items=[_item()])
    R.report_memory_writes("m1", items=[_item(handle="m-2")])
    R.register_turn(message_id="m1", episode_id="ep1", expects_memory=True)
    assert len(persisted) == 1


def test_a_failed_pipeline_is_reported_as_a_memory_failure(persisted):
    R.register_turn(message_id="m1", episode_id="ep1", expects_memory=True)
    R.report_memory_writes("m1", failed=True)
    summary = persisted[0][1]
    assert summary.state == "settled"
    assert summary.status == "failed"


def test_the_watchdog_settles_a_turn_whose_pipeline_never_reports(persisted, monkeypatch):
    # Otherwise a crashed pipeline leaves the card pending forever.
    monkeypatch.setattr(R, "MEMORY_WAIT_SECONDS", 0.05)
    R.register_turn(message_id="m1", episode_id="ep1", expects_memory=True)

    deadline = time.monotonic() + 2.0
    while not persisted and time.monotonic() < deadline:
        time.sleep(0.02)

    assert len(persisted) == 1
    assert persisted[0][1].state == "empty"
