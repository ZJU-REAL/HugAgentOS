import asyncio
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from core.db.models import (
    ChatMessage,
    ChatSession,
    EvolutionEpisode,
    MemoryOutbox,
    ProfileMemory,
)
from core.evolution import settlement_runner
from core.evolution import settlement as evolution_settlement
from core.memory.context import MemoryContext
from core.memory.extractors.router import ExtractorType
from core.memory.extractors import writers as W
from core.memory import outbox as O
from core.memory import pipeline as P
from core.memory import service as S
from core.memory import effect_lane as E
from core.memory import profile_store as PS


def _configure_db(db_session, monkeypatch):
    factory = sessionmaker(bind=db_session.get_bind())
    with factory() as db:
        if db.get(ChatSession, "c1") is None:
            db.add(ChatSession(chat_id="c1", user_id="u1", title="memory outbox test"))
            db.commit()
    monkeypatch.setattr(O, "SessionLocal", factory)
    monkeypatch.setattr(E, "SessionLocal", factory)
    monkeypatch.setattr(PS, "SessionLocal", factory)
    return factory


def _memory_settings(**overrides):
    values = {
        "layered_enabled": True,
        "enabled": True,
        "bg_max_concurrency": 2,
        "outbox_lease_s": 30,
        "outbox_max_attempts": 3,
        "outbox_retry_base_s": 0,
        "llm_gate_enabled": True,
        "gate_timeout_s": 1,
        "extract_timeout_s": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _add_assistant_message(factory, message_id: str) -> None:
    with factory() as db:
        db.add(
            ChatMessage(
                message_id=message_id,
                chat_id="c1",
                role="assistant",
                content="ok",
            )
        )
        db.commit()


def test_schedule_persists_before_any_background_task_runs(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    monkeypatch.setattr(P, "settings", SimpleNamespace(memory=_memory_settings()))
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=_memory_settings()))
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
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=_memory_settings()))
    reports = []
    monkeypatch.setattr(
        O, "kick_outbox_drain", lambda: (_ for _ in ()).throw(RuntimeError("loop closed"))
    )
    monkeypatch.setattr(P, "_report_settlement", lambda *_args, **kwargs: reports.append(kwargs))
    ctx = MemoryContext(user_id="u1", message_id="m-wakeup", write_enabled=True)

    P.schedule_post_response_tasks(ctx, "请记住我叫小明", "好的，我会记住。")

    with factory() as db:
        row = db.query(MemoryOutbox).filter_by(message_id="m-wakeup").one()
        assert row.status == "pending"
    assert reports == []


@pytest.mark.asyncio
async def test_restart_worker_finishes_durable_write_and_settlement(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=_memory_settings()))
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
    monkeypatch.setattr(O, "_extract_candidates", fake_extract)
    monkeypatch.setattr(O, "_write_candidate", fake_write)

    def record_settlement(message_id, items=None, failed=False):
        settlements.append((message_id, list(items or []), failed))
        return {"state": "failed" if failed else "settled", "entries": list(items or [])}

    monkeypatch.setattr(O, "_report_settlement", record_settlement)

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
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=_memory_settings()))
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

    monkeypatch.setattr(O, "_write_candidate", fake_write)
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
        O,
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
        O,
        "settings",
        SimpleNamespace(memory=_memory_settings(outbox_max_attempts=2)),
    )
    ctx = MemoryContext(user_id="u1", message_id="m-bad-gate", write_enabled=True)
    job_id = O.enqueue_pipeline_job(ctx, "normal user turn", "normal assistant response")
    _add_assistant_message(factory, "m-bad-gate")

    async def broken_gate(_payload):
        raise O.RetryableMemoryError("gate unavailable")

    settlements = []
    monkeypatch.setattr(O, "_extract_candidates", broken_gate)

    def record_failed_settlement(message_id, items=None, failed=False):
        settlements.append((message_id, failed))
        return {"state": "failed", "entries": list(items or [])}

    monkeypatch.setattr(O, "_report_settlement", record_failed_settlement)

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

    result = O._report_settlement(
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

    result = O._report_settlement(
        "m-watchdog",
        items=[{"layer": "L1", "handle": "identity.name", "text": "小明"}],
    )

    assert result["state"] == "settled"
    assert result["entries"][0]["handle"] == "identity.name"
    settlement_runner.reset_for_tests()


def test_final_ack_and_settlement_receipt_commit_together(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=_memory_settings()))
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

    assert O._finish_success(candidate_id, "atomic-worker", []) == "m-atomic"

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
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=_memory_settings()))
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


@pytest.mark.asyncio
async def test_worker_shutdown_cancels_woken_consumer_and_releases_lease(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    memory_settings = _memory_settings(outbox_poll_interval_s=30)
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=memory_settings))
    monkeypatch.setattr(P, "settings", SimpleNamespace(memory=memory_settings))
    P._bg_semaphore = None
    ctx = MemoryContext(user_id="u1", message_id="m-stop", write_enabled=True)
    job_id = O.enqueue_candidate_job(
        "parent",
        ctx,
        ExtractorType.IDENTITY,
        {"facts": [{"field": "name", "value": "小明"}]},
    )
    started = asyncio.Event()

    async def never_finishes(*_args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(O, "_write_candidate", never_finishes)
    worker = O.MemoryOutboxWorker()
    await worker.start()
    O.kick_outbox_drain()
    await started.wait()
    await worker.stop()

    with factory() as db:
        row = db.get(MemoryOutbox, job_id)
        assert row.status == "retry"
        assert row.lease_owner is None


@pytest.mark.asyncio
async def test_lease_renewal_loss_cancels_the_old_writer(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    memory_settings = _memory_settings(outbox_lease_s=1)
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=memory_settings))
    monkeypatch.setattr(P, "settings", SimpleNamespace(memory=memory_settings))
    P._bg_semaphore = None
    ctx = MemoryContext(user_id="u1", message_id="m-lease", write_enabled=True)
    job_id = O.enqueue_candidate_job(
        "parent",
        ctx,
        ExtractorType.IDENTITY,
        {"facts": [{"field": "name", "value": "小明"}]},
    )
    cancelled = False

    async def cancellable_writer(*_args):
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        finally:
            cancelled = True

    monkeypatch.setattr(O, "_write_candidate", cancellable_writer)
    monkeypatch.setattr(O, "_renew_lease", lambda *_args: False)

    assert await O.drain_outbox(max_jobs=1, worker_id="lease-worker") == 1
    assert cancelled is True
    with factory() as db:
        row = db.get(MemoryOutbox, job_id)
        assert row.status == "processing"
        assert row.lease_owner == "lease-worker"


@pytest.mark.asyncio
async def test_crash_before_ack_reuses_external_effect_receipt(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    memory_settings = _memory_settings()
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=memory_settings))
    monkeypatch.setattr(P, "settings", SimpleNamespace(memory=memory_settings))
    P._bg_semaphore = None
    ctx = MemoryContext(user_id="u1", message_id="m-effect", write_enabled=True)
    job_id = O.enqueue_candidate_job(
        "parent",
        ctx,
        ExtractorType.PROCEDURAL,
        {"procedures": [{"rule": "先核验主体再取数", "strength": "strong"}]},
    )
    external: dict[str, dict] = {}

    async def find_receipt(_ctx, effect_id, *, strict=False):
        return external.get(effect_id)

    async def no_similar(*_args, **_kwargs):
        return None

    async def save_external(*, content, memory_meta, **_kwargs):
        effect_id = memory_meta["outbox_effect_id"]
        external.setdefault(
            effect_id,
            {"id": "mem-1", "memory": content, "metadata": memory_meta},
        )
        return "mem-1"

    monkeypatch.setattr(W, "find_procedure_by_effect_id", find_receipt)
    monkeypatch.setattr(W, "find_similar_procedure", no_similar)
    monkeypatch.setattr(W, "save_procedure_entry", save_external)
    monkeypatch.setattr(
        W,
        "milvus_breaker",
        SimpleNamespace(
            is_open=lambda: False, record_success=lambda: None, record_failure=lambda: None
        ),
    )

    assert O._claim_specific(job_id, "crashed-worker") is True
    with factory() as db:
        row = db.get(MemoryOutbox, job_id)
        db.expunge(row)
    await O._process_candidate(row)
    assert len(external) == 1
    O._release_worker_leases("crashed-worker")

    await O.drain_outbox(max_jobs=1, worker_id="restart-worker")

    assert len(external) == 1
    with factory() as db:
        row = db.get(MemoryOutbox, job_id)
        assert row.status == "succeeded"
        assert row.result_json[0]["action"] == "replay"


@pytest.mark.asyncio
async def test_interactive_long_term_edits_are_admitted_before_store_effect(
    db_session, monkeypatch
):
    factory = _configure_db(db_session, monkeypatch)
    memory_settings = _memory_settings()
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=memory_settings))
    monkeypatch.setattr(P, "settings", SimpleNamespace(memory=memory_settings))
    P._bg_semaphore = None
    ctx = MemoryContext(user_id="u1", message_id="m-edit", write_enabled=True)
    observed = []

    async def profile_effect(_ctx, fields, *, strict=False):
        with factory() as db:
            row = (
                db.query(MemoryOutbox).filter_by(message_id="m-edit", job_kind="profile_edit").one()
            )
            observed.append(("profile", row.status, strict))
        return [{"key": fields[0][0], "value": fields[0][1], "action": "write"}]

    async def memory_effect(memory_id, text, *, strict=False):
        with factory() as db:
            row = (
                db.query(MemoryOutbox).filter_by(message_id="m-edit", job_kind="memory_edit").one()
            )
            observed.append(("memory", row.status, strict))
        return True

    monkeypatch.setattr("core.memory.profile.upsert_fields", profile_effect)
    monkeypatch.setattr("core.memory.service.update_memory", memory_effect)
    profile_job = O.enqueue_profile_edit_job(ctx, "identity.name", "小明")
    memory_job = O.enqueue_memory_edit_job(ctx, "mem-1", "先核验主体")

    assert O.get_outbox_job(profile_job)["status"] == "pending"
    assert (await O.consume_outbox_job(profile_job))["status"] == "succeeded"
    assert (await O.consume_outbox_job(memory_job))["status"] == "succeeded"
    assert observed == [
        ("profile", "processing", True),
        ("memory", "processing", True),
    ]


@pytest.mark.asyncio
async def test_shutdown_waits_for_real_executor_add_then_replay_uses_receipt(
    db_session, monkeypatch
):
    factory = _configure_db(db_session, monkeypatch)
    memory_settings = _memory_settings(outbox_poll_interval_s=30)
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=memory_settings))
    monkeypatch.setattr(P, "settings", SimpleNamespace(memory=memory_settings))
    monkeypatch.setattr(
        S,
        "settings",
        SimpleNamespace(memory=SimpleNamespace(enabled=True)),
    )
    P._bg_semaphore = None
    started = threading.Event()
    release = threading.Event()

    class BlockingMemory:
        def __init__(self):
            self.add_calls = 0
            self.rows = []

        def add(self, messages, *, user_id, metadata, infer, expiration_date):
            self.add_calls += 1
            started.set()
            assert release.wait(timeout=3)
            self.rows.append(
                {
                    "id": "mem-thread-1",
                    "memory": messages[0]["content"],
                    "metadata": metadata,
                }
            )
            return {
                "results": [
                    {"id": "mem-thread-1", "memory": messages[0]["content"], "event": "ADD"}
                ]
            }

        def get_all(self, *, filters, top_k):
            return {"results": list(self.rows)}

    memory = BlockingMemory()

    async def no_similar(*_args, **_kwargs):
        return None

    monkeypatch.setattr(S, "_get_memory", lambda: memory)
    monkeypatch.setattr(W, "find_similar_procedure", no_similar)
    monkeypatch.setattr(
        W,
        "milvus_breaker",
        SimpleNamespace(
            is_open=lambda: False,
            record_success=lambda: None,
            record_failure=lambda: None,
        ),
    )
    ctx = MemoryContext(user_id="u1", message_id="m-thread", write_enabled=True)
    job_id = O.enqueue_candidate_job(
        "parent",
        ctx,
        ExtractorType.PROCEDURAL,
        {"procedures": [{"rule": "先核验主体再取数", "strength": "strong"}]},
    )
    worker = O.MemoryOutboxWorker()
    await worker.start()
    while not started.is_set():
        await asyncio.sleep(0.01)

    stop_task = asyncio.create_task(worker.stop())
    await asyncio.sleep(0.05)
    assert stop_task.done() is False
    with factory() as db:
        row = db.get(MemoryOutbox, job_id)
        assert row.status == "processing"
        assert row.lease_owner == worker.worker_id

    release.set()
    await asyncio.wait_for(stop_task, timeout=2)
    with factory() as db:
        assert db.get(MemoryOutbox, job_id).status == "retry"

    await O.drain_outbox(max_jobs=1, worker_id="thread-restart")

    assert memory.add_calls == 1
    with factory() as db:
        row = db.get(MemoryOutbox, job_id)
        assert row.status == "succeeded"
        assert row.result_json[0]["action"] == "replay"


@pytest.mark.asyncio
async def test_pipeline_reuses_atomic_extraction_checkpoint_after_crash(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    memory_settings = _memory_settings()
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=memory_settings))
    monkeypatch.setattr(P, "settings", SimpleNamespace(memory=memory_settings))
    P._bg_semaphore = None
    ctx = MemoryContext(user_id="u1", message_id="m-checkpoint", write_enabled=True)
    pipeline_id = O.enqueue_pipeline_job(ctx, "请记住我叫小明", "好的")
    _add_assistant_message(factory, "m-checkpoint")
    calls = 0

    async def first_extract(_payload):
        nonlocal calls
        calls += 1
        return {ExtractorType.IDENTITY: {"facts": [{"field": "name", "value": "小明"}]}}

    monkeypatch.setattr(O, "_extract_candidates", first_extract)
    assert O._claim_specific(pipeline_id, "crashed-pipeline") is True
    with factory() as db:
        row = db.get(MemoryOutbox, pipeline_id)
        db.expunge(row)
    first = await O._process_pipeline(row)
    assert len(first["candidate_job_ids"]) == 1
    O._release_worker_leases("crashed-pipeline")

    async def must_not_extract_again(_payload):
        raise AssertionError("durable extraction checkpoint was ignored")

    async def fake_write(*_args):
        return []

    monkeypatch.setattr(O, "_extract_candidates", must_not_extract_again)
    monkeypatch.setattr(O, "_write_candidate", fake_write)
    monkeypatch.setattr(
        O,
        "_report_settlement",
        lambda *_args, **_kwargs: {"state": "settled", "entries": []},
    )
    await O.drain_outbox(max_jobs=3, worker_id="checkpoint-restart")

    assert calls == 1
    with factory() as db:
        children = db.query(MemoryOutbox).filter_by(parent_id=pipeline_id).all()
        assert len(children) == 1
        assert children[0].status == "succeeded"


@pytest.mark.asyncio
async def test_editing_back_to_an_old_value_creates_a_new_operation(db_session, monkeypatch):
    _configure_db(db_session, monkeypatch)
    memory_settings = _memory_settings()
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=memory_settings))
    monkeypatch.setattr(P, "settings", SimpleNamespace(memory=memory_settings))
    P._bg_semaphore = None
    ctx = MemoryContext(user_id="u1", write_enabled=True)
    stored = {"text": "A"}
    effects = []

    async def update(_memory_id, text, *, strict=False):
        effects.append(text)
        stored["text"] = text
        return True

    monkeypatch.setattr(S, "update_memory", update)
    jobs = [
        O.enqueue_memory_edit_job(ctx, "mem-1", "B", operation_id="op-b-1"),
        O.enqueue_memory_edit_job(ctx, "mem-1", "C", operation_id="op-c"),
        O.enqueue_memory_edit_job(ctx, "mem-1", "B", operation_id="op-b-2"),
    ]
    assert len(set(jobs)) == 3
    assert O.enqueue_memory_edit_job(ctx, "mem-1", "B", operation_id="op-b-2") == jobs[-1]

    for job_id in jobs:
        assert (await O.consume_outbox_job(job_id))["status"] == "succeeded"

    assert effects == ["B", "C", "B"]
    assert stored["text"] == "B"


@pytest.mark.asyncio
async def test_profile_effect_receipt_preserves_written_result_after_crash(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    memory_settings = _memory_settings()
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=memory_settings))
    monkeypatch.setattr(P, "settings", SimpleNamespace(memory=memory_settings))
    P._bg_semaphore = None
    monkeypatch.setattr("core.memory.profile._audit_sync_safe", lambda *_args: None)
    monkeypatch.setattr("core.memory.profile._schedule_compact", lambda *_args: None)
    ctx = MemoryContext(user_id="u1", message_id="m-profile-receipt", write_enabled=True)
    job_id = O.enqueue_candidate_job(
        "parent",
        ctx,
        ExtractorType.IDENTITY,
        {"facts": [{"field": "name", "value": "小明"}]},
    )
    assert O._claim_specific(job_id, "profile-crash") is True
    with factory() as db:
        row = db.get(MemoryOutbox, job_id)
        db.expunge(row)
    first_result = await O._process_candidate(row)
    assert first_result[0]["handle"] == "identity.name"
    O._release_worker_leases("profile-crash")

    await O.drain_outbox(max_jobs=1, worker_id="profile-restart")

    with factory() as db:
        row = db.get(MemoryOutbox, job_id)
        assert row.status == "succeeded"
        assert row.result_json == first_result


@pytest.mark.asyncio
async def test_profile_compact_replays_receipt_without_second_llm_call(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    memory_settings = _memory_settings(profile_max_chars=10)
    monkeypatch.setattr("core.memory.profile.settings", SimpleNamespace(memory=memory_settings))

    async def no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr("core.memory.profile.audit_record", no_audit)
    calls = 0

    async def compact_llm(_content, _max_chars):
        nonlocal calls
        calls += 1
        return "abcdefghijk"  # Accepted by 1.1x guard, but still over max.

    monkeypatch.setattr("core.memory.profile._run_compact_llm", compact_llm)
    with factory() as db:
        db.add(
            ProfileMemory(
                user_id="u-compact",
                workspace_id="default",
                content_md="this profile is much too long",
            )
        )
        db.commit()

    from core.memory.profile import compact

    ctx = MemoryContext(user_id="u-compact", write_enabled=True, effect_id="compact-job")
    assert await compact(ctx, strict=True) is True
    assert await compact(ctx, strict=True) is True
    assert calls == 1


def test_settlement_card_and_job_ack_are_one_transaction(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=_memory_settings()))
    ctx = MemoryContext(user_id="u1", message_id="m-card-atomic", write_enabled=True)
    pipeline_id = O.enqueue_pipeline_job(ctx, "remember", "ok")
    with factory() as db:
        pipeline = db.get(MemoryOutbox, pipeline_id)
        pipeline.status = "succeeded"
        settlement_id = O._enqueue_settlement_if_ready(db, "m-card-atomic", O._utcnow())
        settlement = db.get(MemoryOutbox, settlement_id)
        settlement.status = "processing"
        settlement.lease_owner = "atomic-settler"
        settlement.attempts = 1
        db.add(
            ChatMessage(
                message_id="m-card-atomic",
                chat_id="c1",
                role="assistant",
                content="ok",
                extra_data={"evolution": {"state": "empty", "entries": []}},
            )
        )
        db.commit()

    fail_once = True

    def crash_before_commit(_session):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("simulated crash before atomic commit")

    event.listen(factory.class_, "before_commit", crash_before_commit)
    result = {
        "items": [{"handle": "identity.name"}],
        "failed": False,
        "summary": {
            "state": "settled",
            "entries": [{"handle": "identity.name", "text": "小明"}],
        },
    }
    with pytest.raises(RuntimeError, match="simulated crash"):
        O._finish_success(settlement_id, "atomic-settler", result)
    event.remove(factory.class_, "before_commit", crash_before_commit)

    with factory() as db:
        row = db.get(MemoryOutbox, settlement_id)
        message = db.query(ChatMessage).filter_by(message_id="m-card-atomic").one()
        assert row.status == "processing"
        assert message.extra_data["evolution"]["state"] == "empty"

    assert O._finish_success(settlement_id, "atomic-settler", result) == "m-card-atomic"
    with factory() as db:
        row = db.get(MemoryOutbox, settlement_id)
        message = db.query(ChatMessage).filter_by(message_id="m-card-atomic").one()
        assert row.status == "succeeded"
        assert message.extra_data["evolution"] == result["summary"]


@pytest.mark.asyncio
async def test_settlement_without_assistant_message_rolls_back_for_retry(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=_memory_settings()))
    ctx = MemoryContext(user_id="u1", message_id="m-card-missing", write_enabled=True)
    pipeline_id = O.enqueue_pipeline_job(ctx, "remember", "ok")
    with factory() as db:
        db.get(MemoryOutbox, pipeline_id).status = "succeeded"
        settlement_id = O._enqueue_settlement_if_ready(db, "m-card-missing", O._utcnow())
        settlement = db.get(MemoryOutbox, settlement_id)
        settlement.status = "processing"
        settlement.lease_owner = "missing-message-worker"
        settlement.attempts = 1
        db.commit()

    async def fake_settlement(_row):
        return {"summary": {"state": "settled", "entries": []}}

    monkeypatch.setattr(O, "_process_settlement", fake_settlement)
    await O._process_claimed(settlement_id, "missing-message-worker")

    with factory() as db:
        row = db.get(MemoryOutbox, settlement_id)
        assert row.status == "retry"
        assert "assistant message is not durable" in row.last_error


def test_stale_settlement_owner_cannot_overwrite_card(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=_memory_settings()))
    ctx = MemoryContext(user_id="u1", message_id="m-card-owner", write_enabled=True)
    pipeline_id = O.enqueue_pipeline_job(ctx, "remember", "ok")
    with factory() as db:
        db.get(MemoryOutbox, pipeline_id).status = "succeeded"
        settlement_id = O._enqueue_settlement_if_ready(db, "m-card-owner", O._utcnow())
        settlement = db.get(MemoryOutbox, settlement_id)
        settlement.status = "processing"
        settlement.lease_owner = "new-owner"
        settlement.attempts = 2
        db.add(
            ChatMessage(
                message_id="m-card-owner",
                chat_id="c1",
                role="assistant",
                content="ok",
                extra_data={"evolution": {"state": "edited", "entries": []}},
            )
        )
        db.commit()

    stale = {"summary": {"state": "settled", "entries": [{"text": "stale"}]}}
    assert O._finish_success(settlement_id, "old-owner", stale) is None

    with factory() as db:
        row = db.get(MemoryOutbox, settlement_id)
        message = db.query(ChatMessage).filter_by(message_id="m-card-owner").one()
        assert row.status == "processing"
        assert row.lease_owner == "new-owner"
        assert message.extra_data["evolution"]["state"] == "edited"


@pytest.mark.asyncio
async def test_multi_rule_candidate_keeps_each_external_effect_receipt(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    memory_settings = _memory_settings()
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=memory_settings))
    monkeypatch.setattr(P, "settings", SimpleNamespace(memory=memory_settings))
    monkeypatch.setattr(S, "settings", SimpleNamespace(memory=memory_settings))
    P._bg_semaphore = None
    ctx = MemoryContext(user_id="u1", message_id="m-multi-effect", write_enabled=True)
    job_id = O.enqueue_candidate_job(
        "parent",
        ctx,
        ExtractorType.PROCEDURAL,
        {
            "procedures": [
                {"rule": "提交前先核验主体", "strength": "strong"},
                {"rule": "取数前先核验主体", "strength": "strong"},
            ]
        },
    )
    external = {
        "id": "mem-shared",
        "memory": "先核验主体再工作",
        "metadata": {"layer": "L2", "seen_count": 1},
    }
    reinforcements = 0

    async def find_receipt(_ctx, effect_id, *, strict=False):
        receipts = external["metadata"].get("outbox_effect_ids") or []
        if external["metadata"].get("outbox_effect_id") == effect_id or effect_id in receipts:
            return dict(external)
        return None

    async def find_similar(*_args, **_kwargs):
        # Model Milvus bounded-staleness: both searches see metadata from
        # before this candidate's first reinforcement.
        return {
            "id": external["id"],
            "memory": external["memory"],
            "metadata": {"layer": "L2", "seen_count": 1},
        }

    async def reinforce(similar, *, strength="weak", effect_id=None, candidate_receipts=None):
        nonlocal reinforcements
        reinforcements += 1
        meta = similar["metadata"]
        receipts = [*(meta.get("outbox_effect_ids") or []), *(candidate_receipts or [])]
        candidate_id = effect_id.rpartition(":")[0]
        receipts = [r for r in receipts if r.rpartition(":")[0] == candidate_id]
        receipts.append(effect_id)
        external["metadata"] = {
            **meta,
            "seen_count": int(meta.get("seen_count") or 1) + 1,
            "outbox_effect_id": effect_id,
            "outbox_effect_ids": receipts,
        }
        return True

    monkeypatch.setattr(W, "find_procedure_by_effect_id", find_receipt)
    monkeypatch.setattr(W, "find_similar_procedure", find_similar)
    monkeypatch.setattr(W, "reinforce_procedure_entry", reinforce)
    monkeypatch.setattr(
        W,
        "milvus_breaker",
        SimpleNamespace(
            is_open=lambda: False,
            record_success=lambda: None,
            record_failure=lambda: None,
        ),
    )

    assert O._claim_specific(job_id, "multi-crash") is True
    with factory() as db:
        row = db.get(MemoryOutbox, job_id)
        db.expunge(row)
    first = await O._process_candidate(row)
    assert [item["action"] for item in first] == ["reinforce", "reinforce"]
    assert len(external["metadata"]["outbox_effect_ids"]) == 2
    O._release_worker_leases("multi-crash")

    await O.drain_outbox(max_jobs=1, worker_id="multi-restart")

    assert reinforcements == 2
    with factory() as db:
        recovered = db.get(MemoryOutbox, job_id)
        assert recovered.status == "succeeded"
        assert [item["action"] for item in recovered.result_json] == ["replay", "replay"]


@pytest.mark.asyncio
async def test_later_l2_effect_defers_without_spending_an_attempt(db_session, monkeypatch):
    from datetime import timedelta, timezone

    factory = _configure_db(db_session, monkeypatch)
    memory_settings = _memory_settings()
    monkeypatch.setattr(O, "settings", SimpleNamespace(memory=memory_settings))
    monkeypatch.setattr(P, "settings", SimpleNamespace(memory=memory_settings))
    P._bg_semaphore = None
    ctx = MemoryContext(user_id="u1", message_id="m-lane", write_enabled=True)
    older_id = O.enqueue_candidate_job(
        "parent-old",
        ctx,
        ExtractorType.PROCEDURAL,
        {"procedures": [{"rule": "older", "strength": "strong"}]},
    )
    later_id = O.enqueue_candidate_job(
        "parent-new",
        ctx,
        ExtractorType.PROCEDURAL,
        {"procedures": [{"rule": "later", "strength": "strong"}]},
    )
    due = O._utcnow() + timedelta(seconds=0.2)
    with factory() as db:
        older = db.get(MemoryOutbox, older_id)
        older.status = "retry"
        older.next_attempt_at = due
        db.commit()

    writes = 0

    async def fake_write(*_args):
        nonlocal writes
        writes += 1
        return []

    monkeypatch.setattr(O, "_write_candidate", fake_write)
    assert O._claim_specific(later_id, "later-worker") is True
    await O._process_claimed(later_id, "later-worker")

    assert writes == 0
    with factory() as db:
        later = db.get(MemoryOutbox, later_id)
        assert later.status == "retry"
        assert later.attempts == 0
        next_attempt = later.next_attempt_at
        if next_attempt.tzinfo is None:
            next_attempt = next_attempt.replace(tzinfo=timezone.utc)
        due_aware = due if due.tzinfo is not None else due.replace(tzinfo=timezone.utc)
        assert next_attempt >= due_aware

    await asyncio.sleep(0.25)
    order = []

    async def ordered_write(_extractor, data, _ctx):
        order.append(data["procedures"][0]["rule"])
        return []

    monkeypatch.setattr(O, "_write_candidate", ordered_write)
    assert await O.drain_outbox(max_jobs=2, worker_id="single-worker") == 2
    assert order == ["older", "later"]
