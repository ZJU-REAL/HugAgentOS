"""Hook-path observability is written in the background, bounded and lossy."""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest
from core.db.engine import Base
from core.harness.events import Event, EventSink
from core.harness.hooks import Decision, HookBus, HookSpec, HookStage, Invocation
from core.harness.ledger import DurableEventStore
from core.harness.observation_writer import ObservationWriter, flush_observations
from core.harness.usage import MemoryUsageRecorder
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _stage(run_id: str = "run-bg") -> Invocation:
    return Invocation.create(run_id=run_id, stage=HookStage.BEFORE_MODEL, operation_name="model")


@pytest.mark.asyncio
async def test_slow_durable_writes_never_block_the_stage():
    release = threading.Event()

    class SlowRecorder(MemoryUsageRecorder):
        def record_attempts(self, attempts):
            release.wait(5)
            return super().record_attempts(attempts)

    def slow_store(event):
        release.wait(5)
        return event

    usage = SlowRecorder()
    bus = HookBus(event_sink=EventSink(slow_store), usage_recorder=usage)
    bus.register(HookSpec("policy", HookStage.BEFORE_MODEL, lambda invocation: None))
    started = time.monotonic()
    for _ in range(5):
        await bus.run(_stage())
    assert time.monotonic() - started < 0.5
    # The in-memory view is available before anything reaches the store.
    assert len(bus.event_sink.events()) == 5
    assert usage.attempts("run-bg") == ()

    release.set()
    assert flush_observations()
    assert len(usage.attempts("run-bg")) == 5


@pytest.mark.asyncio
async def test_failing_writes_do_not_change_the_outcome(caplog):
    class BrokenRecorder:
        def record_attempts(self, attempts):
            raise RuntimeError("database down")

    def broken_store(event):
        raise RuntimeError("database down")

    bus = HookBus(event_sink=EventSink(broken_store), usage_recorder=BrokenRecorder())
    bus.register(HookSpec("policy", HookStage.BEFORE_MODEL, lambda invocation: Decision.reject("no")))
    with caplog.at_level(logging.WARNING, logger="core.harness.observation_writer"):
        outcome = await bus.run(_stage("run-broken"))
        assert flush_observations()
    assert outcome.decision.reason == "no"
    assert "database down" in caplog.text


def test_full_queue_drops_without_blocking(caplog):
    writer = ObservationWriter(max_pending=2)
    gate = threading.Event()
    written = []

    def write(sink, records):
        gate.wait(5)
        written.extend(records)

    with caplog.at_level(logging.WARNING, logger="core.harness.observation_writer"):
        assert writer.submit("sink", write, "run", ["first"])
        time.sleep(0.05)  # the writer thread is now holding "first"
        started = time.monotonic()
        results = [writer.submit("sink", write, "run", [index]) for index in range(10)]
        assert time.monotonic() - started < 0.1
    assert results[:2] == [True, True] and not any(results[2:])
    assert writer.dropped == 8
    assert "Observation queue full" in caplog.text

    gate.set()
    assert writer.flush(5)
    assert written == ["first", 0, 1]


def test_queued_records_are_batched_per_sink_and_run():
    writer = ObservationWriter()
    gate = threading.Event()
    calls = []

    def write(sink, records):
        gate.wait(5)
        calls.append((sink, list(records)))

    writer.submit("a", write, "blocker", ["x"])
    time.sleep(0.05)
    for record in range(3):
        writer.submit("a", write, "run-1", [record])
        writer.submit("b", write, "run-1", [record])
    writer.submit("a", write, "run-2", ["y"])
    gate.set()
    assert writer.flush(5)
    assert calls == [
        ("a", ["x"]),
        ("a", [0, 1, 2]),
        ("b", [0, 1, 2]),
        ("a", ["y"]),
    ]


@pytest.mark.asyncio
async def test_published_events_reach_the_durable_store_in_order(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'events.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    store = DurableEventStore(sessions)
    sink = EventSink(store)
    for index in range(4):
        published = await sink.publish(
            Event.create(run_id="run-events", event_type=f"e{index}", phase="before_run")
        )
        assert published.event_seq is None
    assert flush_observations()
    batch = store.store_many(
        [Event.create(run_id="run-events", event_type="late", phase="before_run")]
    )
    assert batch[0].event_seq == 5
    from core.db.models import HarnessEventLog

    with sessions() as db:
        rows = db.query(HarnessEventLog).order_by(HarnessEventLog.event_seq).all()
        assert [(row.event_seq, row.event_type) for row in rows] == [
            (1, "e0"), (2, "e1"), (3, "e2"), (4, "e3"), (5, "late"),
        ]
    engine.dispose()
    await asyncio.sleep(0)
