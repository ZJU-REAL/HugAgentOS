"""Harness 4.1 durable ChatRun journal contracts."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from core.db.engine import Base
from core.db.models import ChatMessage, ChatRun, ChatRunOperation, ChatSession
from core.services.run_journal import (
    RunAlreadyExists,
    RunJournal,
    RunLeaseLost,
    durable_run_binding,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture()
def journal_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'journal.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        db.add(ChatSession(chat_id="chat-1", user_id="user-1", title="journal test"))
        db.commit()
    clock = Clock()
    yield RunJournal(sessions, clock=clock), sessions, clock
    engine.dispose()


def _accept(journal: RunJournal, suffix: str = "one"):
    return journal.accept(
        run_id=f"run_{suffix}",
        message_id=f"msg_{suffix}",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat", "message": "hello"},
        recovery_snapshot={
            "kind": "chat",
            "worker_args": {
                "session_messages": [{"role": "user", "content": "hello"}],
                "effective_user_message": "hello",
                "raw_user_message": "hello",
                "context": {"user_id": "user-1"},
                "model_name": "test-model",
            },
        },
    )


def test_accept_is_committed_before_any_worker_can_start(journal_env):
    journal, sessions, _clock = journal_env

    accepted = _accept(journal)

    with sessions() as db:
        row = db.get(ChatRun, accepted.run_id)
        assert row is not None
        assert row.status == "pending"
        assert row.run_phase == "accepted"
        assert row.operation_seq == 0
        assert row.snapshot_version == 1
        assert row.recovery_snapshot["kind"] == "chat"


@pytest.mark.asyncio
async def test_deterministic_binding_identity_admits_only_one_process(journal_env):
    _journal, sessions, _clock = journal_env
    kwargs = {
        "user_id": "user-1",
        "chat_id": "chat-batch",
        "kind": "batch_item",
        "external_id": "plan-1:0:0",
        "request_payload": {"plan_id": "plan-1", "item_index": 0},
        "session_factory": sessions,
        "binding_run_id": "run_batch_deterministic",
    }

    async with durable_run_binding(**kwargs) as first:
        assert first.run_id == "run_batch_deterministic"
        with pytest.raises(RunAlreadyExists):
            async with durable_run_binding(**kwargs):
                pytest.fail("duplicate batch admission unexpectedly entered")

    with sessions() as db:
        rows = (
            db.query(ChatRun).filter(ChatRun.run_id == "run_batch_deterministic").all()
        )
        assert len(rows) == 1
        assert rows[0].status == "completed"


def test_orphaned_plan_generation_stops_for_attention(journal_env):
    journal, sessions, clock = journal_env
    row = journal.accept(
        run_id="run-plan-generate-orphan",
        message_id="msg-plan-generate-orphan",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "plan_generate"},
        recovery_snapshot={"kind": "plan_generate", "worker_args": {}},
    )
    assert journal.claim(row.run_id, owner="dead", lease_seconds=10)
    clock.advance(11)

    decisions = journal.recover()

    assert [(item.run_id, item.action) for item in decisions] == [
        (row.run_id, "needs_attention")
    ]
    with sessions() as db:
        paused = db.get(ChatRun, row.run_id)
        assert paused.status == "needs_attention"


def test_two_workers_racing_to_claim_have_exactly_one_winner(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'claim-race.sqlite'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    clock = Clock()
    _accept(RunJournal(sessions, clock=clock), "race")
    barrier = Barrier(2)

    def claim(owner: str) -> bool:
        barrier.wait(timeout=5)
        return RunJournal(sessions, clock=clock).claim(
            "run_race", owner=owner, lease_seconds=30
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("worker-a", "worker-b")))

    assert sorted(results) == [False, True]
    with sessions() as db:
        row = db.get(ChatRun, "run_race")
        assert row.lease_owner in {"worker-a", "worker-b"}
    engine.dispose()


def test_expired_lease_can_be_taken_over_and_old_owner_is_fenced(journal_env):
    journal, sessions, clock = journal_env
    run = _accept(journal)
    assert journal.claim(run.run_id, owner="old", lease_seconds=30)

    clock.advance(31)
    assert journal.claim(run.run_id, owner="new", lease_seconds=30)
    with pytest.raises(RunLeaseLost):
        journal.append_operation(
            run.run_id,
            owner="old",
            operation_type="late_write",
            phase="pre_model",
            safety="replayable",
        )

    operation = journal.append_operation(
        run.run_id,
        owner="new",
        operation_type="model_dispatch",
        phase="model_inflight",
        safety="replayable",
    )
    assert operation.operation_seq == 1
    with sessions() as db:
        row = db.get(ChatRun, run.run_id)
        assert row.lease_owner == "new"
        assert row.operation_seq == 1


def test_owner_is_fenced_at_the_exact_lease_expiry_boundary(journal_env):
    journal, _sessions, clock = journal_env
    run = _accept(journal, "exact-expiry")
    assert journal.claim(run.run_id, owner="old", lease_seconds=30)

    clock.advance(30)
    assert not journal.renew(run.run_id, owner="old", lease_seconds=30)
    with pytest.raises(RunLeaseLost):
        journal.append_operation(
            run.run_id,
            owner="old",
            operation_type="late_write",
            phase="pre_model",
            safety="replayable",
        )
    assert journal.claim(run.run_id, owner="new", lease_seconds=30)


def test_operations_and_snapshots_advance_monotonically_in_one_journal(journal_env):
    journal, sessions, _clock = journal_env
    run = _accept(journal)
    assert journal.claim(run.run_id, owner="worker", lease_seconds=60)

    first = journal.append_operation(
        run.run_id,
        owner="worker",
        operation_type="worker_started",
        phase="pre_model",
        safety="replayable",
    )
    second = journal.append_operation(
        run.run_id,
        owner="worker",
        operation_type="model_dispatch",
        phase="model_inflight",
        safety="replayable",
    )
    snapshot = journal.save_snapshot(
        run.run_id,
        owner="worker",
        phase="model_completed",
        snapshot={"assistant_content": "done", "message_id": run.message_id},
        safety="replayable",
    )

    assert (first.operation_seq, second.operation_seq) == (1, 2)
    assert snapshot.operation_seq == 3
    assert snapshot.snapshot_version == 2
    with sessions() as db:
        operations = (
            db.query(ChatRunOperation)
            .filter(ChatRunOperation.run_id == run.run_id)
            .order_by(ChatRunOperation.operation_seq)
            .all()
        )
        assert [row.operation_seq for row in operations] == [1, 2, 3]
        assert operations[-1].snapshot_version == 2


def test_terminal_cas_rejects_late_worker_after_cancel(journal_env):
    journal, sessions, _clock = journal_env
    run = _accept(journal)
    assert journal.claim(run.run_id, owner="worker", lease_seconds=60)

    assert journal.cancel(run.run_id, reason="user cancelled")
    assert not journal.complete(
        run.run_id,
        owner="worker",
        status="completed",
        usage={"input_tokens": 1},
    )

    with sessions() as db:
        row = db.get(ChatRun, run.run_id)
        assert row.status == "cancelled"
        assert row.failure_reason == "user cancelled"
        assert row.lease_owner is None


def test_unowned_recovery_cannot_pause_a_run_with_a_live_lease(journal_env):
    journal, sessions, _clock = journal_env
    run = _accept(journal, "live-owner")
    assert journal.claim(run.run_id, owner="worker", lease_seconds=60)

    assert not journal.needs_attention(run.run_id, reason="stale recovery decision")

    with sessions() as db:
        row = db.get(ChatRun, run.run_id)
        assert row.status == "running"
        assert row.lease_owner == "worker"
        assert row.operation_seq == 0


def test_business_write_and_terminal_cas_share_the_owner_transaction(journal_env):
    journal, sessions, clock = journal_env
    run = _accept(journal, "business-fence")
    assert journal.claim(run.run_id, owner="old", lease_seconds=30)

    clock.advance(31)
    assert journal.claim(run.run_id, owner="new", lease_seconds=30)

    def old_write(db):
        db.add(
            ChatMessage(
                message_id=run.message_id,
                chat_id=run.chat_id,
                role="assistant",
                content="stale output",
            )
        )

    assert not journal.complete(
        run.run_id,
        owner="old",
        status="completed",
        commit_effect=old_write,
        committed_operation={
            "operation_type": "message_committed",
            "phase": "message_committed",
            "safety": "side_effect_committed",
            "payload": {"message_id": run.message_id},
        },
    )
    with sessions() as db:
        assert db.get(ChatMessage, run.message_id) is None

    def new_write(db):
        db.add(
            ChatMessage(
                message_id=run.message_id,
                chat_id=run.chat_id,
                role="assistant",
                content="owned output",
            )
        )

    assert journal.complete(
        run.run_id,
        owner="new",
        status="completed",
        commit_effect=new_write,
        committed_operation={
            "operation_type": "message_committed",
            "phase": "message_committed",
            "safety": "side_effect_committed",
            "payload": {"message_id": run.message_id},
        },
    )
    with sessions() as db:
        assert db.get(ChatMessage, run.message_id).content == "owned output"
        operations = (
            db.query(ChatRunOperation)
            .filter(ChatRunOperation.run_id == run.run_id)
            .order_by(ChatRunOperation.operation_seq)
            .all()
        )
        assert [item.operation_type for item in operations] == [
            "message_committed",
            "run_completed",
        ]


def test_commit_effect_failure_rolls_back_business_write_and_journal(journal_env):
    journal, sessions, _clock = journal_env
    run = _accept(journal, "atomic-rollback")
    assert journal.claim(run.run_id, owner="worker", lease_seconds=60)

    def broken_write(db):
        db.add(
            ChatMessage(
                message_id=run.message_id,
                chat_id=run.chat_id,
                role="assistant",
                content="must roll back",
            )
        )
        db.flush()
        raise RuntimeError("simulated commit failure")

    with pytest.raises(RuntimeError, match="simulated commit failure"):
        journal.complete(
            run.run_id,
            owner="worker",
            status="completed",
            commit_effect=broken_write,
            committed_operation={
                "operation_type": "message_committed",
                "phase": "message_committed",
                "safety": "side_effect_committed",
            },
        )

    with sessions() as db:
        row = db.get(ChatRun, run.run_id)
        assert row.status == "running"
        assert row.operation_seq == 0
        assert db.get(ChatMessage, run.message_id) is None


def test_projection_offsets_are_monotonic_across_lease_takeover(journal_env):
    journal, _sessions, clock = journal_env
    run = _accept(journal, "offset")
    assert journal.claim(run.run_id, owner="old", lease_seconds=30)
    assert journal.allocate_event_offset(run.run_id, owner="old") == 1
    assert journal.allocate_event_offset(run.run_id, owner="old") == 2

    clock.advance(31)
    assert journal.claim(run.run_id, owner="new", lease_seconds=30)
    with pytest.raises(RunLeaseLost):
        journal.allocate_event_offset(run.run_id, owner="old")
    assert journal.allocate_event_offset(run.run_id, owner="new") == 3
    assert journal.complete(run.run_id, owner="new", status="completed")
    assert journal.allocate_event_offset(run.run_id, terminal=True) == 4


def test_recovery_classifies_pre_model_model_snapshot_message_commit_and_unknown_tool(
    journal_env,
):
    journal, sessions, clock = journal_env

    accepted = _accept(journal, "accepted")

    pre_model = _accept(journal, "pre")
    assert journal.claim(pre_model.run_id, owner="dead-pre", lease_seconds=10)
    journal.append_operation(
        pre_model.run_id,
        owner="dead-pre",
        operation_type="worker_started",
        phase="pre_model",
        safety="replayable",
    )

    model_inflight = _accept(journal, "model-inflight")
    assert journal.claim(model_inflight.run_id, owner="dead-inflight", lease_seconds=10)
    journal.append_operation(
        model_inflight.run_id,
        owner="dead-inflight",
        operation_type="model_dispatch",
        phase="model_inflight",
        safety="replayable",
    )

    model_done = _accept(journal, "model")
    assert journal.claim(model_done.run_id, owner="dead-model", lease_seconds=10)
    journal.save_snapshot(
        model_done.run_id,
        owner="dead-model",
        phase="model_completed",
        safety="replayable",
        snapshot={"assistant_content": "answer", "message_id": model_done.message_id},
    )

    committed = _accept(journal, "committed")
    assert journal.claim(committed.run_id, owner="dead-commit", lease_seconds=10)
    journal.append_operation(
        committed.run_id,
        owner="dead-commit",
        operation_type="message_committed",
        phase="message_committed",
        safety="side_effect_committed",
    )

    unknown = _accept(journal, "unknown")
    assert journal.claim(unknown.run_id, owner="dead-tool", lease_seconds=10)
    journal.append_operation(
        unknown.run_id,
        owner="dead-tool",
        operation_type="tool_result_observed",
        phase="tool_result_unknown",
        safety="unknown_side_effect",
    )

    clock.advance(11)
    decisions = {item.run_id: item for item in journal.recover()}

    assert decisions[accepted.run_id].action == "resume"
    assert decisions[pre_model.run_id].action == "resume"
    assert decisions[model_inflight.run_id].action == "needs_attention"
    assert decisions[model_done.run_id].action == "resume_from_snapshot"
    assert decisions[model_done.run_id].snapshot["assistant_content"] == "answer"
    assert decisions[committed.run_id].action == "completed"
    assert decisions[unknown.run_id].action == "needs_attention"
    with sessions() as db:
        assert db.get(ChatRun, committed.run_id).status == "completed"
        attention = db.get(ChatRun, unknown.run_id)
        assert attention.status == "needs_attention"
        assert attention.run_phase == "needs_attention"
        assert (
            attention.failure_reason == "unknown tool result requires recovery decision"
        )
        operations = (
            db.query(ChatRunOperation)
            .filter(ChatRunOperation.run_id == unknown.run_id)
            .order_by(ChatRunOperation.operation_seq)
            .all()
        )
        assert [item.operation_type for item in operations] == [
            "tool_result_observed",
            "run_needs_attention",
        ]
        inflight = db.get(ChatRun, model_inflight.run_id)
        assert inflight.status == "needs_attention"
        assert "model outcome is unknown" in inflight.failure_reason


def test_recovery_ignores_non_chat_runs_without_a_journal_snapshot(journal_env):
    journal, sessions, _clock = journal_env
    journal.accept(
        run_id="run-plan",
        message_id="msg-plan",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "plan_execute"},
    )

    assert journal.recover() == []
    with sessions() as db:
        row = db.get(ChatRun, "run-plan")
        assert row.status == "pending"
        assert row.run_phase == "accepted"


def test_recovery_pauses_orphan_internal_tool_run_instead_of_leaving_it_live(
    journal_env,
):
    journal, sessions, _clock = journal_env
    journal.accept(
        run_id="run-internal-agent",
        message_id="msg-internal-agent",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "internal_job_agent", "job_id": "job-1"},
        recovery_snapshot={
            "kind": "internal_job_agent",
            "worker_args": {"context": {"mcp_ids": ["web-search"]}},
        },
    )

    decisions = journal.recover()

    assert [(item.run_id, item.action) for item in decisions] == [
        ("run-internal-agent", "needs_attention")
    ]
    with sessions() as db:
        row = db.get(ChatRun, "run-internal-agent")
        assert row.status == "needs_attention"
        assert "owner-specific retry" in row.failure_reason


@pytest.mark.asyncio
async def test_durable_internal_binding_persists_tool_surface_for_effect_recovery(
    journal_env,
):
    _journal, sessions, _clock = journal_env

    async with durable_run_binding(
        user_id="user-1",
        chat_id="chat-1",
        kind="batch_item",
        external_id="plan-1:0:0",
        recovery_snapshot={
            "worker_args": {
                "context": {
                    "mcp_ids": ["web-search"],
                    "skill_ids": ["research"],
                    "model_name": "test-model",
                }
            }
        },
        session_factory=sessions,
        lease_seconds=60,
    ) as binding:
        with sessions() as db:
            row = db.get(ChatRun, binding.run_id)
            assert row.recovery_snapshot["kind"] == "batch_item"
            assert row.recovery_snapshot["worker_args"]["context"] == {
                "mcp_ids": ["web-search"],
                "skill_ids": ["research"],
                "model_name": "test-model",
            }


@pytest.mark.asyncio
async def test_durable_internal_binding_preserves_wrapped_unknown_tool_outcome(
    journal_env,
):
    _journal, sessions, _clock = journal_env
    from core.services.tool_effect_ledger import ToolOutcomeUnknown

    run_id = ""
    with pytest.raises(ExceptionGroup):
        async with durable_run_binding(
            user_id="user-1",
            chat_id="chat-1",
            kind="internal_job_agent",
            external_id="job-1:item-1:0",
            session_factory=sessions,
            lease_seconds=60,
        ) as binding:
            run_id = binding.run_id
            raise ExceptionGroup(
                "AgentScope tool failure", [ToolOutcomeUnknown("effect-1")]
            )

    with sessions() as db:
        row = db.get(ChatRun, run_id)
        assert row.status == "needs_attention"
        assert "effect-1" in row.failure_reason


@pytest.mark.asyncio
async def test_durable_internal_binding_cancels_old_worker_without_cancelling_successor(
    journal_env,
):
    _journal, sessions, _clock = journal_env
    entered = asyncio.Event()
    observed_run_id = ""

    async def old_worker() -> None:
        nonlocal observed_run_id
        async with durable_run_binding(
            user_id="user-1",
            chat_id="chat-1",
            kind="batch_item",
            external_id="plan-1:0:0",
            session_factory=sessions,
            lease_seconds=60,
            heartbeat_interval=0.01,
        ) as binding:
            observed_run_id = binding.run_id
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(old_worker())
    await entered.wait()
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id == observed_run_id).update(
            {"lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
        )
        db.commit()
    assert RunJournal(sessions).claim(
        observed_run_id, owner="successor", lease_seconds=60
    )

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    with sessions() as db:
        row = db.get(ChatRun, observed_run_id)
        assert row.status == "running"
        assert row.lease_owner == "successor"


def test_recovered_tool_result_never_replays_the_original_model_prompt(journal_env):
    journal, sessions, _clock = journal_env
    run = _accept(journal, "recovered-tool-result")
    assert journal.claim(run.run_id, owner="tool-recovery", lease_seconds=60)
    journal.append_operation(
        run.run_id,
        owner="tool-recovery",
        operation_type="tool_result_committed",
        phase="tool_result_committed",
        safety="reconciled",
    )
    assert journal.release(
        run.run_id,
        owner="tool-recovery",
        reason="effect recovered without a durable model continuation cursor",
    )

    decisions = journal.recover()

    assert [(item.run_id, item.action) for item in decisions] == [
        (run.run_id, "needs_attention")
    ]
    with sessions() as db:
        row = db.get(ChatRun, run.run_id)
        assert row.status == "needs_attention"
        assert "automatic prompt replay is unsafe" in row.failure_reason


def test_legacy_live_chat_without_snapshot_pauses_for_manual_review(journal_env):
    journal, sessions, _clock = journal_env
    journal.accept(
        run_id="run-legacy-chat",
        message_id="msg-legacy-chat",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"message": "legacy request"},
    )
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id == "run-legacy-chat").update(
            {"run_phase": "legacy_unrecoverable"}
        )
        db.commit()

    decisions = journal.recover()
    assert len(decisions) == 1
    assert decisions[0].action == "needs_attention"
    with sessions() as db:
        row = db.get(ChatRun, "run-legacy-chat")
        assert row.status == "needs_attention"
        assert "legacy live run" in row.failure_reason
