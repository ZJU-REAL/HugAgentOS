import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from core.db.models import ChatMessage, MemoryOutbox, ProfileMemory
from core.evolution import settlement as evolution_settlement
from core.memory.context import MemoryContext
from core.memory.extractors.router import ExtractorType
from core.memory.extractors import writers as W
from core.memory import outbox as O
from core.memory.outbox import common as outbox_common
from core.memory.outbox import jobs as outbox_jobs
from core.memory.outbox import leases as outbox_leases
from core.memory.outbox import settlement as outbox_settlement
from core.memory.outbox import worker as outbox_worker

from core.memory import pipeline as P
from core.memory import service as S
from core.memory import backend as memory_backend
from core.memory import effect_lane as E
from core.memory import profile_store as PS


from tests.memory.outbox_test_support import _configure_db, _memory_settings


@pytest.mark.asyncio
async def test_profile_effect_receipt_preserves_written_result_after_crash(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    memory_settings = _memory_settings()
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=memory_settings))
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
    assert outbox_leases._claim_specific(job_id, "profile-crash") is True
    with factory() as db:
        row = db.get(MemoryOutbox, job_id)
        db.expunge(row)
    first_result = await outbox_jobs._process_candidate(row)
    assert first_result[0]["handle"] == "identity.name"
    outbox_leases._release_worker_leases("profile-crash")

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
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=_memory_settings()))
    ctx = MemoryContext(user_id="u1", message_id="m-card-atomic", write_enabled=True)
    pipeline_id = O.enqueue_pipeline_job(ctx, "remember", "ok")
    with factory() as db:
        pipeline = db.get(MemoryOutbox, pipeline_id)
        pipeline.status = "succeeded"
        settlement_id = outbox_settlement._enqueue_settlement_if_ready(
            db, "m-card-atomic", outbox_common._utcnow()
        )
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
        outbox_settlement._finish_success(settlement_id, "atomic-settler", result)
    event.remove(factory.class_, "before_commit", crash_before_commit)

    with factory() as db:
        row = db.get(MemoryOutbox, settlement_id)
        message = db.query(ChatMessage).filter_by(message_id="m-card-atomic").one()
        assert row.status == "processing"
        assert message.extra_data["evolution"]["state"] == "empty"

    assert (
        outbox_settlement._finish_success(settlement_id, "atomic-settler", result)
        == "m-card-atomic"
    )
    with factory() as db:
        row = db.get(MemoryOutbox, settlement_id)
        message = db.query(ChatMessage).filter_by(message_id="m-card-atomic").one()
        assert row.status == "succeeded"
        assert message.extra_data["evolution"] == result["summary"]


@pytest.mark.asyncio
async def test_settlement_without_assistant_message_rolls_back_for_retry(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=_memory_settings()))
    ctx = MemoryContext(user_id="u1", message_id="m-card-missing", write_enabled=True)
    pipeline_id = O.enqueue_pipeline_job(ctx, "remember", "ok")
    with factory() as db:
        db.get(MemoryOutbox, pipeline_id).status = "succeeded"
        settlement_id = outbox_settlement._enqueue_settlement_if_ready(
            db, "m-card-missing", outbox_common._utcnow()
        )
        settlement = db.get(MemoryOutbox, settlement_id)
        settlement.status = "processing"
        settlement.lease_owner = "missing-message-worker"
        settlement.attempts = 1
        db.commit()

    async def fake_settlement(_row):
        return {"summary": {"state": "settled", "entries": []}}

    monkeypatch.setattr(outbox_jobs, "_process_settlement", fake_settlement)
    await outbox_worker._process_claimed(settlement_id, "missing-message-worker")

    with factory() as db:
        row = db.get(MemoryOutbox, settlement_id)
        assert row.status == "retry"
        assert "assistant message is not durable" in row.last_error


def test_stale_settlement_owner_cannot_overwrite_card(db_session, monkeypatch):
    factory = _configure_db(db_session, monkeypatch)
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=_memory_settings()))
    ctx = MemoryContext(user_id="u1", message_id="m-card-owner", write_enabled=True)
    pipeline_id = O.enqueue_pipeline_job(ctx, "remember", "ok")
    with factory() as db:
        db.get(MemoryOutbox, pipeline_id).status = "succeeded"
        settlement_id = outbox_settlement._enqueue_settlement_if_ready(
            db, "m-card-owner", outbox_common._utcnow()
        )
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
    assert outbox_settlement._finish_success(settlement_id, "old-owner", stale) is None

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
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=memory_settings))
    monkeypatch.setattr(P, "settings", SimpleNamespace(memory=memory_settings))
    monkeypatch.setattr(memory_backend, "settings", SimpleNamespace(memory=memory_settings))
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

    assert outbox_leases._claim_specific(job_id, "multi-crash") is True
    with factory() as db:
        row = db.get(MemoryOutbox, job_id)
        db.expunge(row)
    first = await outbox_jobs._process_candidate(row)
    assert [item["action"] for item in first] == ["reinforce", "reinforce"]
    assert len(external["metadata"]["outbox_effect_ids"]) == 2
    outbox_leases._release_worker_leases("multi-crash")

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
    monkeypatch.setattr(outbox_common, "settings", SimpleNamespace(memory=memory_settings))
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
    due = outbox_common._utcnow() + timedelta(seconds=0.2)
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

    monkeypatch.setattr(outbox_jobs, "_write_candidate", fake_write)
    assert outbox_leases._claim_specific(later_id, "later-worker") is True
    await outbox_worker._process_claimed(later_id, "later-worker")

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

    monkeypatch.setattr(outbox_jobs, "_write_candidate", ordered_write)
    assert await O.drain_outbox(max_jobs=2, worker_id="single-worker") == 2
    assert order == ["older", "later"]
