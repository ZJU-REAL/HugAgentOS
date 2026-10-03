import asyncio
import threading
from types import SimpleNamespace

import pytest

from core.db.models import MemoryOutbox
from core.evolution import settlement as evolution_settlement
from core.memory.context import MemoryContext
from core.memory.extractors.router import ExtractorType
from core.memory.extractors import writers as W
from core.memory import outbox as O
from core.memory.outbox import common as outbox_common
from core.memory.outbox import jobs as outbox_jobs
from core.memory.outbox import leases as outbox_leases

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


@pytest.mark.asyncio
async def test_worker_shutdown_cancels_woken_consumer_and_releases_lease(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    memory_settings = _memory_settings(outbox_poll_interval_s=30)
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=memory_settings))
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

    monkeypatch.setattr(outbox_jobs, "_write_candidate", never_finishes)
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
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=memory_settings))
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

    monkeypatch.setattr(outbox_jobs, "_write_candidate", cancellable_writer)
    monkeypatch.setattr(outbox_leases, "_renew_lease", lambda *_args: False)

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
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=memory_settings))
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

    assert outbox_leases._claim_specific(job_id, "crashed-worker") is True
    with factory() as db:
        row = db.get(MemoryOutbox, job_id)
        db.expunge(row)
    await outbox_jobs._process_candidate(row)
    assert len(external) == 1
    outbox_leases._release_worker_leases("crashed-worker")

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
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=memory_settings))
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
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=memory_settings))
    monkeypatch.setattr(P, "settings", SimpleNamespace(memory=memory_settings))
    monkeypatch.setattr(
        memory_backend,
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

    monkeypatch.setattr(memory_backend, "_get_memory", lambda: memory)
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
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=memory_settings))
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

    monkeypatch.setattr(outbox_jobs, "_extract_candidates", first_extract)
    assert outbox_leases._claim_specific(pipeline_id, "crashed-pipeline") is True
    with factory() as db:
        row = db.get(MemoryOutbox, pipeline_id)
        db.expunge(row)
    first = await outbox_jobs._process_pipeline(row)
    assert len(first["candidate_job_ids"]) == 1
    outbox_leases._release_worker_leases("crashed-pipeline")

    async def must_not_extract_again(_payload):
        raise AssertionError("durable extraction checkpoint was ignored")

    async def fake_write(*_args):
        return []

    monkeypatch.setattr(outbox_jobs, "_extract_candidates", must_not_extract_again)
    monkeypatch.setattr(outbox_jobs, "_write_candidate", fake_write)
    monkeypatch.setattr(
        outbox_jobs,
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
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=memory_settings))
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
