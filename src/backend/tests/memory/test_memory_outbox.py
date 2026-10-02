from types import SimpleNamespace

import pytest

from core.db.models import EvolutionEpisode, MemoryOutbox
from core.evolution import settlement_runner
from core.evolution import settlement as evolution_settlement
from core.memory.context import MemoryContext
from core.memory.extractors.router import ExtractorType
from core.memory.extractors import writers as W
from core.memory import outbox as O
from core.memory.outbox import common as outbox_common
from core.memory.outbox import jobs as outbox_jobs
from core.memory.outbox import settlement as outbox_settlement

from core.memory import pipeline as P
from core.memory import service as S
from core.memory import backend as memory_backend
from core.memory import effect_lane as E
from core.memory import profile_store as PS


from tests.memory.outbox_test_support import (
    _configure_db,
    _memory_settings,
    _add_assistant_message,
)


def test_schedule_persists_before_any_background_task_runs(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    monkeypatch.setattr(P, "settings", SimpleNamespace(memory=_memory_settings()))
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=_memory_settings()))
    ctx = MemoryContext(
        user_id="u1",
        chat_id="c1",
        message_id="m1",
        write_enabled=True,
    )

    P.schedule_post_response_tasks(ctx, "请记住我叫小明", "好的，我会记住。")

    with factory() as db:
        rows = db.query(MemoryOutbox).all()
        assert len(rows) == 1
        assert rows[0].status == "pending"
        assert rows[0].job_kind == "pipeline"
        assert rows[0].message_id == "m1"


def test_wakeup_failure_does_not_reverse_durable_admission(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    monkeypatch.setattr(P, "settings", SimpleNamespace(memory=_memory_settings()))
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=_memory_settings()))
    reports = []
    wakeups = []

    def fail_wakeup():
        wakeups.append(True)
        raise RuntimeError("loop closed")

    monkeypatch.setattr(O, "kick_outbox_drain", fail_wakeup)
    monkeypatch.setattr(P, "_report_settlement", lambda *_args, **kwargs: reports.append(kwargs))
    ctx = MemoryContext(user_id="u1", message_id="m-wakeup", write_enabled=True)

    P.schedule_post_response_tasks(ctx, "请记住我叫小明", "好的，我会记住。")

    with factory() as db:
        row = db.query(MemoryOutbox).filter_by(message_id="m-wakeup").one()
        assert row.status == "pending"
    assert reports == []
    assert wakeups == [True]


@pytest.mark.asyncio
async def test_restart_worker_finishes_durable_write_and_settlement(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=_memory_settings()))
    ctx = MemoryContext(
        user_id="u1",
        chat_id="c1",
        message_id="m-restart",
        write_enabled=True,
    )
    O.enqueue_pipeline_job(ctx, "请记住我叫小明", "好的，我会记住。")
    _add_assistant_message(factory, "m-restart")

    async def fake_extract(_payload):
        return {ExtractorType.IDENTITY: {"facts": [{"field": "name", "value": "小明"}]}}

    writes = []

    async def fake_write(extractor, data, _ctx):
        writes.append((extractor, data))
        return [
            {
                "layer": "L1",
                "kind": "identity",
                "handle": "identity.name",
                "text": "小明",
                "action": "write",
            }
        ]

    settlements = []
    monkeypatch.setattr(outbox_jobs, "_extract_candidates", fake_extract)
    monkeypatch.setattr(outbox_jobs, "_write_candidate", fake_write)

    def record_settlement(message_id, items=None, failed=False):
        settlements.append((message_id, list(items or []), failed))
        return {"state": "failed" if failed else "settled", "entries": list(items or [])}

    monkeypatch.setattr(outbox_jobs, "_report_settlement", record_settlement)

    processed = await O.drain_outbox(max_jobs=10, worker_id="restart-worker")

    assert processed == 3
    assert len(writes) == 1
    assert settlements == [
        (
            "m-restart",
            [
                {
                    "layer": "L1",
                    "kind": "identity",
                    "handle": "identity.name",
                    "text": "小明",
                    "action": "write",
                }
            ],
            False,
        )
    ]
    with factory() as db:
        assert {row.status for row in db.query(MemoryOutbox).all()} == {"succeeded"}


@pytest.mark.asyncio
async def test_same_candidate_consumed_twice_writes_once(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=_memory_settings()))
    ctx = MemoryContext(user_id="u1", message_id="m-idem", write_enabled=True)
    data = {"facts": [{"field": "name", "value": "小明"}]}

    first = O.enqueue_candidate_job("parent", ctx, ExtractorType.IDENTITY, data)
    second = O.enqueue_candidate_job("parent", ctx, ExtractorType.IDENTITY, data)
    assert first == second

    writes = 0

    async def fake_write(_extractor, _data, _ctx):
        nonlocal writes
        writes += 1
        return []

    monkeypatch.setattr(outbox_jobs, "_write_candidate", fake_write)
    await O.drain_outbox(max_jobs=10, worker_id="w1")
    await O.drain_outbox(max_jobs=10, worker_id="w2")

    assert writes == 1
    with factory() as db:
        row = db.get(MemoryOutbox, first)
        assert row.status == "succeeded"


@pytest.mark.asyncio
async def test_strict_writer_failure_is_not_marked_succeeded(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    monkeypatch.setattr(
        outbox_common,
        "settings",
        SimpleNamespace(memory=_memory_settings(outbox_max_attempts=1)),
    )
    ctx = MemoryContext(user_id="u1", message_id="m-writer-fail", write_enabled=True)
    job_id = O.enqueue_candidate_job(
        "parent",
        ctx,
        ExtractorType.IDENTITY,
        {"facts": [{"field": "name", "value": "小明"}]},
    )

    async def broken_upsert(*_args, **_kwargs):
        raise RuntimeError("profile database unavailable")

    monkeypatch.setattr("core.memory.profile.upsert_fields", broken_upsert)
    await O.drain_outbox(max_jobs=1, worker_id="strict-writer")

    with factory() as db:
        row = db.get(MemoryOutbox, job_id)
        assert row.status == "quarantined"
        assert "profile database unavailable" in row.last_error


@pytest.mark.asyncio
async def test_gate_failure_retries_then_quarantines_and_settles_failed(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    monkeypatch.setattr(
        outbox_common,
        "settings",
        SimpleNamespace(memory=_memory_settings(outbox_max_attempts=2)),
    )
    ctx = MemoryContext(user_id="u1", message_id="m-bad-gate", write_enabled=True)
    job_id = O.enqueue_pipeline_job(ctx, "normal user turn", "normal assistant response")
    _add_assistant_message(factory, "m-bad-gate")

    async def broken_gate(_payload):
        raise O.RetryableMemoryError("gate unavailable")

    settlements = []
    monkeypatch.setattr(outbox_jobs, "_extract_candidates", broken_gate)

    def record_failed_settlement(message_id, items=None, failed=False):
        settlements.append((message_id, failed))
        return {"state": "failed", "entries": list(items or [])}

    monkeypatch.setattr(outbox_jobs, "_report_settlement", record_failed_settlement)

    await O.drain_outbox(max_jobs=1, worker_id="w1")
    with factory() as db:
        assert db.get(MemoryOutbox, job_id).status == "retry"

    await O.drain_outbox(max_jobs=5, worker_id="w2")
    with factory() as db:
        row = db.get(MemoryOutbox, job_id)
        assert row.status == "quarantined"
        assert "gate unavailable" in row.last_error
    assert settlements == [("m-bad-gate", True)]


def test_recovered_settlement_uses_durable_episode_and_retires_watchdog(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    with factory() as db:
        db.add(EvolutionEpisode(episode_id="ep1", message_id="m-recovered"))
        db.commit()

    calls = []
    monkeypatch.setattr(
        settlement_runner,
        "acknowledge_durable_settlement",
        lambda message_id: calls.append(("ack", message_id)),
    )
    summary = SimpleNamespace(to_dict=lambda: {"state": "failed"})
    monkeypatch.setattr(
        evolution_settlement,
        "settle_turn",
        lambda **kwargs: calls.append(("settle", kwargs)) or summary,
    )

    result = outbox_jobs._report_settlement(
        "m-recovered",
        items=[{"layer": "L1", "handle": "identity.name"}],
        failed=True,
    )

    assert result == {"state": "failed"}
    assert calls[0] == ("ack", "m-recovered")
    assert calls[1][0] == "settle"
    assert calls[1][1]["episode_id"] == "ep1"
    assert calls[1][1]["memory_entries"][0]["handle"] == "identity.name"
    assert calls[1][1]["memory_failed"] is True


def test_durable_settlement_overwrites_a_watchdog_result(db_session, monkeypatch):
    _configure_db(db_session, monkeypatch)
    settlement_runner.reset_for_tests()
    settlement_runner._settled["m-watchdog"] = None
    summary = SimpleNamespace(
        to_dict=lambda: {
            "state": "settled",
            "entries": [{"handle": "identity.name"}],
        }
    )
    monkeypatch.setattr(evolution_settlement, "settle_turn", lambda **_kwargs: summary)

    result = outbox_jobs._report_settlement(
        "m-watchdog",
        items=[{"layer": "L1", "handle": "identity.name", "text": "小明"}],
    )

    assert result["state"] == "settled"
    assert result["entries"][0]["handle"] == "identity.name"
    settlement_runner.reset_for_tests()


def test_final_ack_and_settlement_receipt_commit_together(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=_memory_settings()))
    ctx = MemoryContext(user_id="u1", message_id="m-atomic", write_enabled=True)
    pipeline_id = O.enqueue_pipeline_job(ctx, "remember this", "acknowledged")
    candidate_id = O.enqueue_candidate_job(
        pipeline_id,
        ctx,
        ExtractorType.IDENTITY,
        {"facts": [{"field": "name", "value": "小明"}]},
    )
    with factory() as db:
        pipeline = db.get(MemoryOutbox, pipeline_id)
        pipeline.status = "succeeded"
        candidate = db.get(MemoryOutbox, candidate_id)
        candidate.status = "processing"
        candidate.lease_owner = "atomic-worker"
        candidate.attempts = 1
        db.commit()

    assert outbox_settlement._finish_success(candidate_id, "atomic-worker", []) == "m-atomic"

    with factory() as db:
        candidate = db.get(MemoryOutbox, candidate_id)
        settlements = (
            db.query(MemoryOutbox).filter_by(message_id="m-atomic", job_kind="settlement").all()
        )
        assert candidate.status == "succeeded"
        assert len(settlements) == 1
        assert settlements[0].status == "pending"


def test_startup_reconciles_terminal_message_without_settlement(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=_memory_settings()))
    ctx = MemoryContext(user_id="u1", message_id="m-reconcile", write_enabled=True)
    pipeline_id = O.enqueue_pipeline_job(ctx, "remember this", "acknowledged")
    with factory() as db:
        db.get(MemoryOutbox, pipeline_id).status = "succeeded"
        db.commit()

    assert O.reconcile_settlement_jobs() == 1
    assert O.reconcile_settlement_jobs() == 0
    with factory() as db:
        assert (
            db.query(MemoryOutbox)
            .filter_by(message_id="m-reconcile", job_kind="settlement")
            .count()
            == 1
        )
