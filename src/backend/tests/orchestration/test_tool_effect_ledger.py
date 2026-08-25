"""Durable, idempotent tool side-effect execution and recovery."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from threading import Barrier

import pytest
from agentscope.message import TextBlock, ToolCallBlock, ToolResultState
from agentscope.message import UserMsg
from agentscope.agent import Agent, ReActConfig
from agentscope.model import ChatResponse, ChatUsage
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.tool import FunctionTool, Toolkit
from agentscope.tool._response import ToolChunk, ToolResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

from core.db.engine import Base
from core.db.models import (
    ChatRun,
    ChatSession,
    ScheduledTask,
    ToolCallLog,
    ToolEffectLease,
    ToolEffectLedger,
    ToolEffectReceipt,
)
from core.services.run_journal import RunJournal
from core.services.tool_effect_ledger import (
    DEFAULT_TOOL_RECOVERY_REGISTRY,
    ToolEffectError,
    ToolEffectGateway,
    ToolEffectJournal,
    ToolIdempotencyConflict,
    ToolIntent,
    ToolIntentCommitError,
    ToolOutcomeUnknown,
    ToolRecoveryRegistry,
    recover_incomplete_tool_effects,
)
from core.llm.middlewares import AgentRuntimeState, ToolEffectMiddleware
from orchestration.tool_effect_recovery import (
    _plain_replay_args,
    reconcile_scheduled_task_intent,
)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture()
def effect_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tool-effects.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def make_run(
        run_id: str,
        *,
        owner: str = "worker",
        user_id: str = "user-1",
        chat_id: str = "chat-1",
    ) -> None:
        with sessions() as db:
            if db.get(ChatSession, chat_id) is None:
                db.add(ChatSession(chat_id=chat_id, user_id=user_id, title="test"))
                db.commit()
        journal = RunJournal(sessions)
        journal.accept(
            run_id=run_id,
            message_id=f"msg-{run_id}",
            chat_id=chat_id,
            user_id=user_id,
            request_payload={"kind": "chat"},
            recovery_snapshot={"kind": "chat", "worker_args": {}},
        )
        assert journal.claim(run_id, owner=owner, lease_seconds=300)

    yield sessions, make_run
    engine.dispose()


@pytest.mark.asyncio
async def test_intent_commit_failure_never_calls_tool_function():
    invoked = 0

    class BrokenJournal:
        def begin_intent(self, **_kwargs):
            raise ToolIntentCommitError("database unavailable")

    async def tool():
        nonlocal invoked
        invoked += 1
        return {"ok": True}

    gateway = ToolEffectGateway(BrokenJournal(), ToolRecoveryRegistry())
    with pytest.raises(ToolIntentCommitError):
        await gateway.execute(
            run_id="run-1",
            owner="worker",
            tool_call_id="call-1",
            tool_name="write",
            args={"path": "/tmp/a"},
            invoke=tool,
        )
    assert invoked == 0


@pytest.mark.asyncio
async def test_timeout_leaves_outcome_pending_for_policy_recovery(effect_env):
    sessions, make_run = effect_env
    make_run("run-timeout-unknown")
    registry = ToolRecoveryRegistry()
    registry.register("remote_write", "reconcile", reconciler=lambda _intent: None)
    gateway = ToolEffectGateway(ToolEffectJournal(sessions), registry)

    async def timed_out():
        raise TimeoutError("response deadline exceeded")

    with pytest.raises(ToolOutcomeUnknown) as raised:
        await gateway.execute(
            run_id="run-timeout-unknown",
            owner="worker",
            tool_call_id="timeout-call",
            tool_name="remote_write",
            args={"value": 1},
            invoke=timed_out,
        )
    assert isinstance(raised.value.__cause__, TimeoutError)
    with sessions() as db:
        assert [
            row.event_type
            for row in db.query(ToolEffectLedger)
            .filter(ToolEffectLedger.run_id == "run-timeout-unknown")
            .order_by(ToolEffectLedger.event_id)
        ] == ["intent"]


@pytest.mark.asyncio
async def test_concurrent_same_idempotency_key_executes_once(effect_env):
    sessions, make_run = effect_env
    make_run("run-concurrent")
    registry = ToolRecoveryRegistry()
    registry.register("safe_read", "replay_safe")
    ledger = ToolEffectJournal(sessions)
    gateway = ToolEffectGateway(ledger, registry, poll_interval=0.005)
    invoked = 0

    async def tool():
        nonlocal invoked
        invoked += 1
        await asyncio.sleep(0.05)
        return {"value": 42}

    async def call():
        return await gateway.execute(
            run_id="run-concurrent",
            owner="worker",
            tool_call_id="provider-call-a",
            tool_name="safe_read",
            args={"query": "same"},
            idempotency_key="stable-key",
            invoke=tool,
        )

    first, second = await asyncio.gather(call(), call())
    assert first == second == {"value": 42}
    assert invoked == 1
    with sessions() as db:
        events = db.query(ToolEffectLedger).order_by(ToolEffectLedger.created_at).all()
        assert [event.event_type for event in events] == ["intent", "result"]


def test_sqlite_concurrent_same_key_prepares_one_effect(effect_env):
    sessions, make_run = effect_env
    make_run("run-sqlite-same-key")
    barrier = Barrier(2)

    def prepare(claim_owner: str):
        barrier.wait()
        return ToolEffectJournal(sessions).begin_intent(
            run_id="run-sqlite-same-key",
            owner="worker",
            claim_owner=claim_owner,
            tool_call_id="same-provider-call",
            tool_name="Read",
            args={"path": "/workspace/a.txt"},
            recovery_policy="replay_safe",
            idempotency_key="sqlite-same-key",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(prepare, ("claim-a", "claim-b")))
    assert sorted(item.action for item in decisions) == ["execute", "wait"]
    assert len({item.effect_id for item in decisions}) == 1


def test_sqlite_concurrent_distinct_calls_both_commit_intents(effect_env):
    sessions, make_run = effect_env
    make_run("run-sqlite-distinct")
    barrier = Barrier(2)

    def prepare(suffix: str):
        barrier.wait()
        return ToolEffectJournal(sessions).begin_intent(
            run_id="run-sqlite-distinct",
            owner="worker",
            claim_owner=f"claim-{suffix}",
            tool_call_id=f"provider-{suffix}",
            tool_name="Read",
            args={"path": f"/workspace/{suffix}.txt"},
            recovery_policy="replay_safe",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(prepare, ("a", "b")))
    assert [item.action for item in decisions] == ["execute", "execute"]
    assert len({item.effect_id for item in decisions}) == 2


@pytest.mark.asyncio
async def test_slow_tools_do_not_hold_database_connections_during_io(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'one-connection.sqlite'}",
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        db.add_all(
            [
                ChatSession(chat_id="pool-chat-a", user_id="pool-user", title="a"),
                ChatSession(chat_id="pool-chat-b", user_id="pool-user", title="b"),
            ]
        )
        db.commit()
    journal = RunJournal(sessions)
    for suffix in ("a", "b"):
        journal.accept(
            run_id=f"pool-run-{suffix}",
            message_id=f"pool-message-{suffix}",
            chat_id=f"pool-chat-{suffix}",
            user_id="pool-user",
            request_payload={"kind": "test"},
            recovery_snapshot={"kind": "test"},
        )
        assert journal.claim(f"pool-run-{suffix}", owner=f"pool-owner-{suffix}", lease_seconds=30)

    registry = ToolRecoveryRegistry()
    registry.register("slow_read", "replay_safe")
    gateway = ToolEffectGateway(ToolEffectJournal(sessions), registry)
    active = 0
    max_active = 0

    async def slow_tool():
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.1)
        active -= 1
        return {"ok": True}

    async def call(suffix: str):
        return await gateway.execute(
            run_id=f"pool-run-{suffix}",
            owner=f"pool-owner-{suffix}",
            tool_call_id=f"pool-call-{suffix}",
            tool_name="slow_read",
            args={"suffix": suffix},
            invoke=slow_tool,
        )

    try:
        results = await asyncio.wait_for(asyncio.gather(call("a"), call("b")), timeout=2)
        assert results == [{"ok": True}, {"ok": True}]
        assert max_active == 2
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_distinct_provider_calls_with_same_args_are_distinct_effects(effect_env):
    sessions, make_run = effect_env
    make_run("run-distinct")
    registry = ToolRecoveryRegistry()
    registry.register("write", "never_replay")
    gateway = ToolEffectGateway(ToolEffectJournal(sessions), registry)
    invoked = 0

    async def tool():
        nonlocal invoked
        invoked += 1
        return {"call": invoked}

    first = await gateway.execute(
        run_id="run-distinct",
        owner="worker",
        tool_call_id="call-a",
        tool_name="write",
        args={"path": "/same"},
        invoke=tool,
    )
    second = await gateway.execute(
        run_id="run-distinct",
        owner="worker",
        tool_call_id="call-b",
        tool_name="write",
        args={"path": "/same"},
        invoke=tool,
    )
    assert first == {"call": 1}
    assert second == {"call": 2}
    assert invoked == 2


@pytest.mark.asyncio
async def test_parallel_distinct_tools_commit_both_results_on_sqlite(effect_env):
    sessions, make_run = effect_env
    make_run("run-parallel-distinct")
    registry = ToolRecoveryRegistry()
    registry.register("Read", "replay_safe")
    gateway = ToolEffectGateway(ToolEffectJournal(sessions), registry)
    entered = 0
    both_entered = asyncio.Event()

    async def tool(value: str):
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await both_entered.wait()
        return {"value": value}

    async def call(suffix: str):
        return await gateway.execute(
            run_id="run-parallel-distinct",
            owner="worker",
            tool_call_id=f"parallel-{suffix}",
            tool_name="Read",
            args={"path": f"/{suffix}"},
            invoke=lambda: tool(suffix),
        )

    results = await asyncio.gather(call("a"), call("b"))
    assert results == [{"value": "a"}, {"value": "b"}]
    with sessions() as db:
        assert (
            db.query(ToolEffectLedger).filter(ToolEffectLedger.event_type == "result").count() == 2
        )


def test_sqlite_concurrent_distinct_result_commits_are_retried(effect_env):
    sessions, make_run = effect_env
    make_run("run-concurrent-results")
    ledger = ToolEffectJournal(sessions)
    intents = [
        ledger.begin_intent(
            run_id="run-concurrent-results",
            owner="worker",
            claim_owner=f"claim-{suffix}",
            tool_call_id=f"result-{suffix}",
            tool_name="Read",
            args={"path": f"/{suffix}"},
            recovery_policy="replay_safe",
        )
        for suffix in ("a", "b")
    ]
    barrier = Barrier(2)

    def commit(index: int):
        barrier.wait()
        return ToolEffectJournal(sessions).commit_result(
            intents[index].effect_id,
            run_owner="worker",
            claim_owner=f"claim-{'ab'[index]}",
            result={"value": "ab"[index]},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(commit, (0, 1)))
    assert results == [{"value": "a"}, {"value": "b"}]


@pytest.mark.asyncio
async def test_explicit_key_is_principal_scoped_and_rejects_different_arguments(effect_env):
    sessions, make_run = effect_env
    make_run("run-key-a", owner="worker-a")
    make_run("run-key-b", owner="worker-b")
    registry = ToolRecoveryRegistry()
    registry.register("safe", "replay_safe")
    gateway = ToolEffectGateway(ToolEffectJournal(sessions), registry)
    invoked = 0

    async def tool():
        nonlocal invoked
        invoked += 1
        return {"ok": True}

    await gateway.execute(
        run_id="run-key-a",
        owner="worker-a",
        tool_call_id="call-a",
        tool_name="safe",
        args={"value": 1},
        idempotency_key="global-key",
        invoke=tool,
    )
    result = await gateway.execute(
        run_id="run-key-b",
        owner="worker-b",
        tool_call_id="call-b",
        tool_name="safe",
        args={"value": 1},
        idempotency_key="global-key",
        invoke=tool,
    )
    assert result == {"ok": True}
    assert invoked == 1
    with pytest.raises(ToolIdempotencyConflict):
        await gateway.execute(
            run_id="run-key-b",
            owner="worker-b",
            tool_call_id="call-c",
            tool_name="safe",
            args={"value": 2},
            idempotency_key="global-key",
            invoke=tool,
        )

    make_run(
        "run-key-other-user",
        owner="worker-other",
        user_id="user-2",
        chat_id="chat-2",
    )
    other_result = await gateway.execute(
        run_id="run-key-other-user",
        owner="worker-other",
        tool_call_id="call-other",
        tool_name="safe",
        args={"value": 1},
        idempotency_key="global-key",
        invoke=tool,
    )
    assert other_result == {"ok": True}
    assert invoked == 2


@pytest.mark.asyncio
async def test_expired_replay_safe_intent_is_replayed(effect_env):
    sessions, make_run = effect_env
    make_run("run-replay")
    clock = MutableClock()
    registry = ToolRecoveryRegistry()
    registry.register("safe_read", "replay_safe")
    ledger = ToolEffectJournal(sessions, clock=clock)
    first = ledger.begin_intent(
        run_id="run-replay",
        owner="worker",
        claim_owner="crashed-invocation",
        tool_call_id="old-provider-call",
        tool_name="safe_read",
        args={"query": "recover me"},
        recovery_policy="replay_safe",
        idempotency_key="recover-key",
        lease_seconds=1,
    )
    assert first.action == "execute"
    clock.advance(2)
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id == "run-replay").update(
            {"lease_expires_at": clock() - timedelta(seconds=1)}
        )
        db.commit()
    assert RunJournal(sessions, clock=clock).claim(
        "run-replay", owner="recovery-worker", lease_seconds=300
    )
    invoked = 0

    async def tool():
        nonlocal invoked
        invoked += 1
        return {"replayed": True}

    result = await ToolEffectGateway(ledger, registry).execute(
        run_id="run-replay",
        owner="recovery-worker",
        tool_call_id="new-provider-call",
        tool_name="safe_read",
        args={"query": "recover me"},
        idempotency_key="recover-key",
        invoke=tool,
    )
    assert result == {"replayed": True}
    assert invoked == 1


@pytest.mark.asyncio
async def test_startup_replayer_does_not_alias_a_later_same_args_provider_call(effect_env):
    sessions, make_run = effect_env
    make_run("run-startup-replay")
    clock = MutableClock()
    replayed = 0

    async def replay(_intent):
        nonlocal replayed
        replayed += 1
        return {"from_adapter": True}

    registry = ToolRecoveryRegistry()
    registry.register("safe_read", "replay_safe", replayer=replay)
    ledger = ToolEffectJournal(sessions, clock=clock)
    original = ledger.begin_intent(
        run_id="run-startup-replay",
        owner="worker",
        claim_owner="crashed",
        tool_call_id="old-provider-id",
        tool_name="safe_read",
        args={"query": "same"},
        recovery_policy="replay_safe",
        lease_seconds=1,
    )
    clock.advance(2)
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id == "run-startup-replay").update(
            {"lease_expires_at": clock() - timedelta(seconds=1)}
        )
        db.commit()
    decisions = await recover_incomplete_tool_effects(journal=ledger, registry=registry)
    assert [(item.effect_id, item.action) for item in decisions] == [
        (original.effect_id, "replayed")
    ]
    assert RunJournal(sessions, clock=clock).claim(
        "run-startup-replay", owner="new-worker", lease_seconds=300
    )
    invoked = 0

    async def later_call():
        nonlocal invoked
        invoked += 1
        return {"wrong": True}

    outcome = await ToolEffectGateway(ledger, registry).execute_outcome(
        run_id="run-startup-replay",
        owner="new-worker",
        tool_call_id="new-provider-id",
        tool_name="safe_read",
        args={"query": "same"},
        invoke=later_call,
    )
    assert outcome.result == {"wrong": True}
    assert outcome.effect_id != original.effect_id
    assert outcome.invoked is True
    assert replayed == 1
    assert invoked == 1


@pytest.mark.asyncio
async def test_invocation_heartbeat_keeps_long_call_claimed(effect_env):
    sessions, make_run = effect_env
    make_run("run-fenced", owner="old-worker")
    registry = ToolRecoveryRegistry()
    registry.register("safe", "replay_safe")
    ledger = ToolEffectJournal(sessions)
    gateway = ToolEffectGateway(ledger, registry, poll_interval=0.005, lease_seconds=1)
    entered = asyncio.Event()
    release = asyncio.Event()
    invoked = 0

    async def slow_tool():
        nonlocal invoked
        invoked += 1
        entered.set()
        await release.wait()
        return {"ok": True}

    first = asyncio.create_task(
        gateway.execute(
            run_id="run-fenced",
            owner="old-worker",
            tool_call_id="call-old",
            tool_name="safe",
            args={"q": 1},
            idempotency_key="fenced-key",
            invoke=slow_tool,
        )
    )
    await entered.wait()
    await asyncio.sleep(1.2)
    assert not RunJournal(sessions).claim("run-fenced", owner="new-worker", lease_seconds=300)
    assert invoked == 1
    release.set()
    assert await first == {"ok": True}
    assert invoked == 1


def test_sqlite_atomic_recovery_claim_has_one_winner(effect_env):
    sessions, make_run = effect_env
    make_run("run-sqlite-cas")
    ledger = ToolEffectJournal(sessions)
    intent = ledger.begin_intent(
        run_id="run-sqlite-cas",
        owner="worker",
        claim_owner="crashed",
        tool_call_id="sqlite-cas-call",
        tool_name="safe",
        args={"q": 1},
        recovery_policy="replay_safe",
        lease_seconds=1,
    )
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id == "run-sqlite-cas").update(
            {"lease_expires_at": expired}
        )
        db.query(ToolEffectLease).filter(ToolEffectLease.effect_id == intent.effect_id).update(
            {"lease_expires_at": expired}
        )
        db.commit()

    barrier = Barrier(2)

    def claim(owner: str):
        barrier.wait()
        return ToolEffectJournal(sessions).claim_recovery_intent(
            intent.effect_id,
            recovery_owner=owner,
            lease_seconds=30,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("recovery-a", "recovery-b")))
    assert sum(result is not None for result in results) == 1


@pytest.mark.asyncio
async def test_reconcile_intent_queries_adapter_before_execution(effect_env):
    sessions, make_run = effect_env
    make_run("run-reconcile")
    clock = MutableClock()
    reconciled = 0

    async def reconcile(intent):
        nonlocal reconciled
        reconciled += 1
        assert intent.redacted_args == {"amount": 8}
        return {"external_status": "already_applied"}

    registry = ToolRecoveryRegistry()
    registry.register("charge_account", "reconcile", reconciler=reconcile)
    ledger = ToolEffectJournal(sessions, clock=clock)
    ledger.begin_intent(
        run_id="run-reconcile",
        owner="worker",
        claim_owner="crashed-invocation",
        tool_call_id="old-call",
        tool_name="charge_account",
        args={"amount": 8},
        recovery_policy="reconcile",
        idempotency_key="charge-key",
        lease_seconds=1,
    )
    clock.advance(2)
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id == "run-reconcile").update(
            {"lease_expires_at": clock() - timedelta(seconds=1)}
        )
        db.commit()
    assert RunJournal(sessions, clock=clock).claim(
        "run-reconcile", owner="recovery-worker", lease_seconds=300
    )
    invoked = 0

    async def must_not_execute():
        nonlocal invoked
        invoked += 1
        return {"wrong": True}

    result = await ToolEffectGateway(ledger, registry).execute(
        run_id="run-reconcile",
        owner="recovery-worker",
        tool_call_id="new-call",
        tool_name="charge_account",
        args={"amount": 8},
        idempotency_key="charge-key",
        invoke=must_not_execute,
    )
    assert result == {"external_status": "already_applied"}
    assert reconciled == 1
    assert invoked == 0


@pytest.mark.asyncio
async def test_unknown_tool_defaults_never_replay_and_pauses_run(effect_env):
    sessions, make_run = effect_env
    make_run("run-unknown")
    clock = MutableClock()
    registry = ToolRecoveryRegistry()
    assert registry.resolve("unregistered_write").policy == "never_replay"
    ledger = ToolEffectJournal(sessions, clock=clock)
    ledger.begin_intent(
        run_id="run-unknown",
        owner="worker",
        claim_owner="crashed-invocation",
        tool_call_id="old-call",
        tool_name="unregistered_write",
        args={"target": "outside"},
        recovery_policy="never_replay",
        idempotency_key="unknown-key",
        lease_seconds=1,
    )
    clock.advance(2)
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id == "run-unknown").update(
            {"lease_expires_at": clock() - timedelta(seconds=1)}
        )
        db.commit()
    assert RunJournal(sessions, clock=clock).claim(
        "run-unknown", owner="recovery-worker", lease_seconds=300
    )
    invoked = 0

    async def must_not_execute():
        nonlocal invoked
        invoked += 1
        return {"wrong": True}

    with pytest.raises(ToolOutcomeUnknown):
        await ToolEffectGateway(ledger, registry).execute(
            run_id="run-unknown",
            owner="recovery-worker",
            tool_call_id="new-call",
            tool_name="unregistered_write",
            args={"target": "outside"},
            idempotency_key="unknown-key",
            invoke=must_not_execute,
        )
    assert invoked == 0
    with sessions() as db:
        assert db.get(ChatRun, "run-unknown").status == "needs_attention"
        events = (
            db.query(ToolEffectLedger)
            .filter(ToolEffectLedger.run_id == "run-unknown")
            .order_by(ToolEffectLedger.created_at)
            .all()
        )
        assert [event.event_type for event in events] == [
            "intent",
            "recovery_claim",
            "unknown_outcome",
        ]
    with pytest.raises(ToolOutcomeUnknown):
        await ToolEffectGateway(ledger, registry).execute(
            run_id="run-unknown",
            owner="recovery-worker",
            tool_call_id="another-call",
            tool_name="unregistered_write",
            args={"target": "outside"},
            idempotency_key="unknown-key",
            invoke=must_not_execute,
        )


def test_result_commit_retry_is_idempotent_and_args_are_redacted(effect_env):
    sessions, make_run = effect_env
    make_run("run-result")
    ledger = ToolEffectJournal(sessions)
    prepared = ledger.begin_intent(
        run_id="run-result",
        owner="worker",
        claim_owner="invocation",
        tool_call_id="call-result",
        tool_name="write",
        args={"path": "/tmp/a", "password": "super-secret"},
        recovery_policy="never_replay",
        idempotency_key="result-key",
    )
    first = ledger.commit_result(
        prepared.effect_id,
        run_owner="worker",
        claim_owner="invocation",
        result={"ok": True},
    )
    second = ledger.commit_result(
        prepared.effect_id,
        run_owner="worker",
        claim_owner="invocation",
        result={"ok": True},
    )
    assert first == second == {"ok": True}
    with sessions() as db:
        events = (
            db.query(ToolEffectLedger)
            .filter(ToolEffectLedger.effect_id == prepared.effect_id)
            .order_by(ToolEffectLedger.created_at)
            .all()
        )
        assert [event.event_type for event in events] == ["intent", "result"]
        assert events[0].redacted_args["password"] != "super-secret"
        assert "super-secret" not in str(events[0].redacted_args)
        assert len(events[0].args_hash) == 64


def test_concurrent_result_commit_retry_is_idempotent(effect_env):
    sessions, make_run = effect_env
    make_run("run-result-race")
    ledger = ToolEffectJournal(sessions)
    prepared = ledger.begin_intent(
        run_id="run-result-race",
        owner="worker",
        claim_owner="same-claim",
        tool_call_id="result-race",
        tool_name="Read",
        args={"path": "/a"},
        recovery_policy="replay_safe",
    )
    barrier = Barrier(2)

    def commit():
        barrier.wait(timeout=5)
        return ledger.commit_result(
            prepared.effect_id,
            run_owner="worker",
            claim_owner="same-claim",
            result={"ok": True},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _index: commit(), range(2))) == [
            {"ok": True},
            {"ok": True},
        ]
    with sessions() as db:
        assert (
            db.query(ToolEffectLedger)
            .filter(
                ToolEffectLedger.effect_id == prepared.effect_id,
                ToolEffectLedger.event_type == "result",
            )
            .count()
            == 1
        )


def test_default_recovery_policies_have_real_adapters():
    assert DEFAULT_TOOL_RECOVERY_REGISTRY.resolve("Read").replayer is not None
    scheduled = DEFAULT_TOOL_RECOVERY_REGISTRY.resolve("create_scheduled_task")
    assert scheduled.policy == "reconcile"
    assert scheduled.reconciler is not None
    assert scheduled.replayer is not None
    assert DEFAULT_TOOL_RECOVERY_REGISTRY.resolve("unregistered_write").policy == "never_replay"


def test_recovery_rejects_nested_withheld_arguments():
    intent = ToolIntent(
        effect_id="eff-nested-secret",
        result_id="result-nested-secret",
        run_id="run-nested-secret",
        operation_seq=1,
        tool_name="Read",
        tool_call_id="call-nested-secret",
        args_hash="hash",
        redacted_args={
            "request": {"steps": [{"command": {"_withheld": True, "sha256": "abc", "length": 3}}]}
        },
        idempotency_key="key",
        recovery_policy="replay_safe",
    )
    with pytest.raises(ToolEffectError, match="withheld sensitive free text"):
        _plain_replay_args(intent)


@pytest.mark.asyncio
async def test_scheduled_create_reconciliation_uses_exact_atomic_receipt(
    effect_env,
    monkeypatch,
):
    sessions, make_run = effect_env
    make_run("run-scheduled-receipt")
    ledger = ToolEffectJournal(sessions)
    intent = ledger.begin_intent(
        run_id="run-scheduled-receipt",
        owner="worker",
        claim_owner="scheduled-call",
        tool_call_id="scheduled-call",
        tool_name="create_scheduled_task",
        args={
            "cron_expression": "0 9 * * *",
            "prompt": "send report",
            "name": "daily",
        },
        recovery_policy="reconcile",
    ).intent
    with sessions() as db:
        db.add(
            ScheduledTask(
                task_id="pre-existing-same-content",
                user_id="user-1",
                task_type="prompt",
                prompt="send report",
                cron_expression="0 9 * * *",
                name="daily",
                status="active",
            )
        )
        db.commit()

    import core.db.engine as engine_module
    import orchestration.tool_effect_recovery as recovery_module
    from mcp_servers.automation_task_mcp import impl

    monkeypatch.setattr(engine_module, "SessionLocal", sessions)
    monkeypatch.setattr(recovery_module, "SessionLocal", sessions)
    before = await reconcile_scheduled_task_intent(intent)
    assert before.outcome == "not_applied"

    result = impl.create_task(
        user_id="user-1",
        cron_expression="0 9 * * *",
        prompt="send report",
        name="daily",
        tool_effect_id=intent.effect_id,
    )
    assert result["ok"] is True
    again = impl.create_task(
        user_id="user-1",
        cron_expression="0 9 * * *",
        prompt="send report",
        name="daily",
        tool_effect_id=intent.effect_id,
    )
    assert again == result
    after = await reconcile_scheduled_task_intent(intent)
    assert after.outcome == "applied"
    assert after.result == result
    with sessions() as db:
        assert db.query(ScheduledTask).count() == 2
        assert db.get(ToolEffectReceipt, intent.effect_id).result_payload == result


def test_embedded_secrets_and_free_text_are_not_persisted(effect_env):
    sessions, make_run = effect_env
    make_run("run-secrets")
    secret = "sk-123456789012345678901234567890"
    ledger = ToolEffectJournal(sessions)
    intent = ledger.begin_intent(
        run_id="run-secrets",
        owner="worker",
        claim_owner="invocation",
        tool_call_id="secret-call",
        tool_name="Bash",
        args={
            "command": f"curl -H 'Authorization: Bearer {secret}' https://x",
            "url": f"https://example.test/?token={secret}",
        },
        recovery_policy="never_replay",
    )
    with sessions() as db:
        row = (
            db.query(ToolEffectLedger)
            .filter(
                ToolEffectLedger.effect_id == intent.effect_id,
                ToolEffectLedger.event_type == "intent",
            )
            .one()
        )
        assert secret not in json.dumps(row.redacted_args)
        assert row.redacted_args["command"]["_withheld"] is True
        assert "[REDACTED]" in row.redacted_args["url"]


@pytest.mark.asyncio
async def test_missing_run_binding_fails_closed_before_tool_invocation(effect_env):
    sessions, _make_run = effect_env
    middleware = ToolEffectMiddleware(session_factory=sessions)
    agent = SimpleNamespace(state=SimpleNamespace(run_id=None, journal_owner=None))
    invoked = 0

    async def tool(**_kwargs):
        nonlocal invoked
        invoked += 1
        yield ToolResponse(content=[TextBlock(text="wrong")])

    with pytest.raises(ToolIntentCommitError):
        _ = [
            item
            async for item in middleware.on_acting(
                agent,
                {"tool_call": ToolCallBlock(id="x", name="Read", input="{}")},
                tool,
            )
        ]
    assert invoked == 0


@pytest.mark.asyncio
async def test_middleware_leaves_adapter_exception_pending_for_recovery(effect_env):
    sessions, make_run = effect_env
    make_run("run-tool-failure")
    middleware = ToolEffectMiddleware(session_factory=sessions)
    agent = SimpleNamespace(
        state=SimpleNamespace(
            run_id="run-tool-failure",
            journal_owner="worker",
            tool_effect_links={},
        )
    )

    async def broken(**_kwargs):
        if False:
            yield None
        raise RuntimeError("adapter exploded")

    with pytest.raises(ToolOutcomeUnknown) as raised:
        _ = [
            item
            async for item in middleware.on_acting(
                agent,
                {"tool_call": ToolCallBlock(id="failure-call", name="Read", input="{}")},
                broken,
            )
        ]
    assert isinstance(raised.value.__cause__, RuntimeError)
    with sessions() as db:
        events = db.query(ToolEffectLedger).order_by(ToolEffectLedger.event_id).all()
        assert [event.event_type for event in events] == ["intent"]


@pytest.mark.asyncio
async def test_cancelled_run_settles_pending_intent_without_delayed_replay(effect_env):
    sessions, make_run = effect_env
    make_run("run-cancelled-effect")
    registry = ToolRecoveryRegistry()
    invoked = 0

    async def replay(_intent):
        nonlocal invoked
        invoked += 1
        return {"should_not": "run"}

    registry.register("Read", "replay_safe", replayer=replay)
    ledger = ToolEffectJournal(sessions)
    ledger.begin_intent(
        run_id="run-cancelled-effect",
        owner="worker",
        claim_owner="dead-call",
        tool_call_id="cancelled-call",
        tool_name="Read",
        args={"path": "/workspace/a.txt"},
        recovery_policy="replay_safe",
    )
    assert RunJournal(sessions).cancel("run-cancelled-effect", reason="user cancelled")

    decisions = await recover_incomplete_tool_effects(journal=ledger, registry=registry)

    assert [item.action for item in decisions] == ["terminal_unknown"]
    assert invoked == 0
    with sessions() as db:
        run = db.get(ChatRun, "run-cancelled-effect")
        events = (
            db.query(ToolEffectLedger)
            .filter(ToolEffectLedger.run_id == run.run_id)
            .order_by(ToolEffectLedger.event_id)
            .all()
        )
        assert run.status == "cancelled"
        assert run.run_phase == "cancelled"
        assert [item.event_type for item in events] == ["intent", "unknown_outcome"]


@pytest.mark.asyncio
async def test_middleware_commits_error_tool_response_as_failure(effect_env):
    sessions, make_run = effect_env
    make_run("run-tool-error-response")
    middleware = ToolEffectMiddleware(session_factory=sessions)
    agent = SimpleNamespace(
        state=SimpleNamespace(
            run_id="run-tool-error-response",
            journal_owner="worker",
            tool_effect_links={},
        )
    )

    async def failed_response(**_kwargs):
        yield ToolResponse(
            content=[TextBlock(text="tool rejected input")],
            state=ToolResultState.ERROR,
        )

    output = [
        item
        async for item in middleware.on_acting(
            agent,
            {"tool_call": ToolCallBlock(id="error-response-call", name="Read", input="{}")},
            failed_response,
        )
    ]
    assert output[-1].state == ToolResultState.ERROR
    with sessions() as db:
        events = db.query(ToolEffectLedger).order_by(ToolEffectLedger.event_id).all()
        assert [event.event_type for event in events] == ["intent", "failure"]


@pytest.mark.asyncio
async def test_middleware_returns_explicit_error_for_unregistered_tool(effect_env):
    sessions, make_run = effect_env
    make_run("run-unregistered-tool-error")
    middleware = ToolEffectMiddleware(session_factory=sessions)
    agent = SimpleNamespace(
        state=SimpleNamespace(
            run_id="run-unregistered-tool-error",
            journal_owner="worker",
            tool_effect_links={},
        )
    )
    original_error = "Remote API returned 404: resource not found or not authorized"

    async def failed_response(**_kwargs):
        yield ToolResponse(
            content=[TextBlock(text=original_error)],
            state=ToolResultState.ERROR,
        )

    output = [
        item
        async for item in middleware.on_acting(
            agent,
            {
                "tool_call": ToolCallBlock(
                    id="unregistered-error-call",
                    name="remote_resource_lookup",
                    input='{"resource":"missing-or-private"}',
                )
            },
            failed_response,
        )
    ]

    assert output[-1].state == ToolResultState.ERROR
    assert original_error in str(output[-1].content)
    with sessions() as db:
        events = db.query(ToolEffectLedger).order_by(ToolEffectLedger.event_id).all()
        assert [event.event_type for event in events] == ["intent", "failure"]
        assert events[0].recovery_policy == "never_replay"
        assert original_error in str(events[-1].result_payload)


@pytest.mark.asyncio
async def test_agentscope_middleware_commits_intent_before_call_and_replays_same_shape(
    effect_env,
):
    sessions, make_run = effect_env
    make_run("run-middleware")
    middleware = ToolEffectMiddleware(session_factory=sessions)
    agent = SimpleNamespace(state=SimpleNamespace(run_id="run-middleware", journal_owner="worker"))
    invoked = 0
    saw_durable_intent = False

    async def execute_once(**_kwargs):
        nonlocal invoked, saw_durable_intent
        invoked += 1
        with sessions() as db:
            saw_durable_intent = (
                db.query(ToolEffectLedger)
                .filter(
                    ToolEffectLedger.run_id == "run-middleware",
                    ToolEffectLedger.event_type == "intent",
                )
                .count()
                == 1
            )
        block = TextBlock(text="same SSE-compatible result")
        yield ToolChunk(content=[block], state=ToolResultState.RUNNING)
        yield ToolResponse(content=[block], state=ToolResultState.SUCCESS)

    first_call = ToolCallBlock(
        id="provider-call-1",
        name="Read",
        input='{"path":"/workspace/a.txt"}',
    )
    first = [
        item
        async for item in middleware.on_acting(
            agent,
            {"tool_call": first_call},
            execute_once,
        )
    ]

    async def must_not_execute(**_kwargs):
        nonlocal invoked
        invoked += 1
        yield ToolResponse(content=[TextBlock(text="wrong")])

    replay_call = ToolCallBlock(
        id="provider-call-1",
        name="Read",
        input='{"path":"/workspace/a.txt"}',
    )
    replayed = [
        item
        async for item in middleware.on_acting(
            agent,
            {"tool_call": replay_call},
            must_not_execute,
        )
    ]

    assert saw_durable_intent is True
    assert invoked == 1
    assert [type(item) for item in first] == [ToolChunk, ToolResponse]
    assert [type(item) for item in replayed] == [ToolChunk, ToolResponse]
    assert replayed[-1].state == ToolResultState.SUCCESS
    assert replayed[-1].content[0].text == "same SSE-compatible result"


@pytest.mark.asyncio
async def test_real_agentscope_public_reply_commits_intent_before_adapter(effect_env):
    sessions, make_run = effect_env
    make_run("run-public-agent")
    saw_intent = False
    invoked = 0

    async def probe(value: int) -> ToolChunk:
        """Return a test value."""
        nonlocal saw_intent, invoked
        invoked += 1
        with sessions() as db:
            saw_intent = (
                db.query(ToolEffectLedger)
                .filter(
                    ToolEffectLedger.run_id == "run-public-agent",
                    ToolEffectLedger.event_type == "intent",
                )
                .count()
                == 1
            )
        return ToolChunk(
            content=[TextBlock(text=f"value={value}")],
            state=ToolResultState.SUCCESS,
        )

    class ToolThenAnswerModel:
        model = "tool-then-answer"
        context_size = 32768

        def __init__(self):
            self.calls = 0

        async def __call__(self, messages, tools=None, **_kwargs):
            del messages
            self.calls += 1
            if self.calls == 1:
                assert any(item["function"]["name"] == "probe" for item in tools)
                return ChatResponse(
                    content=[ToolCallBlock(id="public-call", name="probe", input='{"value":7}')],
                    is_last=False,
                    usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.01),
                )
            return ChatResponse(
                content=[TextBlock(text="done")],
                is_last=True,
                usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.01),
            )

        async def count_tokens(self, messages, tools=None):
            del messages, tools
            return 1

    state = AgentRuntimeState(
        run_id="run-public-agent",
        journal_owner="worker",
        permission_context=PermissionContext(mode=PermissionMode.BYPASS),
    )
    agent = Agent(
        name="ledger-public-seam",
        system_prompt="Use the probe tool once.",
        model=ToolThenAnswerModel(),
        toolkit=Toolkit(tools=[FunctionTool(probe)]),
        middlewares=[ToolEffectMiddleware(session_factory=sessions)],
        state=state,
        react_config=ReActConfig(max_iters=2),
    )
    reply = await agent.reply(UserMsg(name="user", content="run probe"))
    assert reply.get_text_content() == "done"
    assert invoked == 1
    assert saw_intent is True
    with sessions() as db:
        assert [
            row.event_type for row in db.query(ToolEffectLedger).order_by(ToolEffectLedger.event_id)
        ] == ["intent", "result"]


@pytest.mark.asyncio
async def test_public_reply_cancellation_stops_tool_without_late_terminal_write(effect_env):
    sessions, make_run = effect_env
    make_run("run-public-cancel")
    entered = asyncio.Event()
    release = asyncio.Event()
    side_effects = 0

    async def blocking_probe(value: int) -> ToolChunk:
        """Wait until the caller releases the test tool."""
        nonlocal side_effects
        entered.set()
        await release.wait()
        side_effects += value
        return ToolChunk(
            content=[TextBlock(text=f"value={value}")],
            state=ToolResultState.SUCCESS,
        )

    class BlockingToolModel:
        model = "blocking-tool"
        context_size = 32768

        async def __call__(self, messages, tools=None, **_kwargs):
            del messages
            assert any(item["function"]["name"] == "blocking_probe" for item in tools)
            return ChatResponse(
                content=[
                    ToolCallBlock(
                        id="cancel-call",
                        name="blocking_probe",
                        input='{"value":1}',
                    )
                ],
                is_last=False,
                usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.01),
            )

        async def count_tokens(self, messages, tools=None):
            del messages, tools
            return 1

    agent = Agent(
        name="ledger-cancel-seam",
        system_prompt="Use the blocking probe.",
        model=BlockingToolModel(),
        toolkit=Toolkit(tools=[FunctionTool(blocking_probe)]),
        middlewares=[ToolEffectMiddleware(session_factory=sessions)],
        state=AgentRuntimeState(
            run_id="run-public-cancel",
            journal_owner="worker",
            permission_context=PermissionContext(mode=PermissionMode.BYPASS),
        ),
        react_config=ReActConfig(max_iters=2),
    )
    reply_task = asyncio.create_task(agent.reply(UserMsg(name="user", content="start")))
    await entered.wait()
    assert RunJournal(sessions).cancel("run-public-cancel", reason="test cancellation")
    reply_task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await reply_task
    assert side_effects == 0
    with sessions() as db:
        run = db.get(ChatRun, "run-public-cancel")
        assert run.status == "cancelled"
        assert run.run_phase == "cancelled"
        assert [
            row.event_type
            for row in db.query(ToolEffectLedger)
            .filter(ToolEffectLedger.run_id == "run-public-cancel")
            .order_by(ToolEffectLedger.event_id)
        ] == ["intent"]


@pytest.mark.asyncio
async def test_restart_sweep_classifies_replay_reconcile_and_never_replay(effect_env):
    sessions, make_run = effect_env
    clock = MutableClock()
    registry = ToolRecoveryRegistry()

    async def replay(_intent):
        return {"replayed": True}

    registry.register("safe_read", "replay_safe", replayer=replay)

    async def reconcile(_intent):
        return {"external_status": "committed"}

    registry.register("external_write", "reconcile", reconciler=reconcile)
    ledger = ToolEffectJournal(sessions, clock=clock)
    effects = {}
    for run_id, tool_name, policy in (
        ("run-sweep-replay", "safe_read", "replay_safe"),
        ("run-sweep-reconcile", "external_write", "reconcile"),
        ("run-sweep-never", "unknown_write", "never_replay"),
    ):
        make_run(run_id)
        effects[run_id] = ledger.begin_intent(
            run_id=run_id,
            owner="worker",
            claim_owner="crashed-tool",
            tool_call_id=f"call-{run_id}",
            tool_name=tool_name,
            args={"target": run_id},
            recovery_policy=policy,
            idempotency_key=f"key-{run_id}",
            lease_seconds=1,
        ).effect_id
    clock.advance(2)
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id.in_(effects)).update(
            {"lease_expires_at": clock() - timedelta(seconds=1)},
            synchronize_session=False,
        )
        db.commit()

    decisions = await recover_incomplete_tool_effects(journal=ledger, registry=registry)
    assert {(item.run_id, item.action) for item in decisions} == {
        ("run-sweep-replay", "replayed"),
        ("run-sweep-reconcile", "reconciled"),
        ("run-sweep-never", "needs_attention"),
    }
    with sessions() as db:
        assert db.get(ChatRun, "run-sweep-replay").status == "running"
        assert db.get(ChatRun, "run-sweep-reconcile").run_phase == "tool_result_committed"
        assert db.get(ChatRun, "run-sweep-never").status == "needs_attention"
        reconciled = (
            db.query(ToolEffectLedger)
            .filter(
                ToolEffectLedger.effect_id == effects["run-sweep-reconcile"],
                ToolEffectLedger.event_type == "result",
            )
            .one()
        )
        assert reconciled.result_payload == {"external_status": "committed"}


@pytest.mark.asyncio
async def test_recovery_adapter_failure_does_not_starve_later_intent(effect_env):
    sessions, make_run = effect_env
    clock = MutableClock()
    registry = ToolRecoveryRegistry()

    async def broken(_intent):
        raise RuntimeError("temporary adapter outage")

    async def healthy(_intent):
        return {"ok": True}

    registry.register("broken_read", "replay_safe", replayer=broken)
    registry.register("healthy_read", "replay_safe", replayer=healthy)
    ledger = ToolEffectJournal(sessions, clock=clock)
    effects = []
    for run_id, tool_name in (("run-broken", "broken_read"), ("run-healthy", "healthy_read")):
        make_run(run_id)
        effects.append(
            ledger.begin_intent(
                run_id=run_id,
                owner="worker",
                claim_owner="crashed",
                tool_call_id=f"call-{run_id}",
                tool_name=tool_name,
                args={"q": run_id},
                recovery_policy="replay_safe",
                lease_seconds=1,
            ).effect_id
        )
    clock.advance(2)
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id.in_(["run-broken", "run-healthy"])).update(
            {"lease_expires_at": clock() - timedelta(seconds=1)}, synchronize_session=False
        )
        db.commit()
    decisions = await recover_incomplete_tool_effects(journal=ledger, registry=registry)
    assert {(item.run_id, item.action) for item in decisions} == {
        ("run-broken", "retry"),
        ("run-healthy", "replayed"),
    }
    assert ledger.terminal_decision(effects[0]) is None
    assert ledger.terminal_decision(effects[1]).result == {"ok": True}
    run_recovery = RunJournal(sessions, clock=clock).recover()
    assert "run-broken" not in {item.run_id for item in run_recovery}


@pytest.mark.asyncio
async def test_recovery_heartbeat_prevents_second_sweep_from_replaying_long_adapter(effect_env):
    sessions, make_run = effect_env
    make_run("run-long-recovery")
    ledger = ToolEffectJournal(sessions)
    intent = ledger.begin_intent(
        run_id="run-long-recovery",
        owner="worker",
        claim_owner="crashed",
        tool_call_id="long-recovery-call",
        tool_name="long_read",
        args={"query": "slow"},
        recovery_policy="replay_safe",
        lease_seconds=1,
    )
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    with sessions() as db:
        db.query(ChatRun).filter(ChatRun.run_id == "run-long-recovery").update(
            {"lease_expires_at": expired}
        )
        db.query(ToolEffectLease).filter(ToolEffectLease.effect_id == intent.effect_id).update(
            {"lease_expires_at": expired}
        )
        db.commit()

    started = asyncio.Event()
    release = asyncio.Event()
    replays = 0

    async def replay(_intent):
        nonlocal replays
        replays += 1
        started.set()
        await release.wait()
        return {"ok": True}

    registry = ToolRecoveryRegistry()
    registry.register("long_read", "replay_safe", replayer=replay)
    first = asyncio.create_task(
        recover_incomplete_tool_effects(
            journal=ledger,
            registry=registry,
            lease_seconds=1,
            heartbeat_interval=0.1,
        )
    )
    await started.wait()
    await asyncio.sleep(1.1)
    second = await recover_incomplete_tool_effects(
        journal=ledger,
        registry=registry,
        lease_seconds=1,
        heartbeat_interval=0.1,
    )
    assert second == []
    assert replays == 1
    release.set()
    decisions = await first
    assert [(item.effect_id, item.action) for item in decisions] == [(intent.effect_id, "replayed")]


def test_tool_call_log_is_a_projection_linked_to_authoritative_effect(effect_env, monkeypatch):
    sessions, make_run = effect_env
    make_run("run-projection")
    ledger = ToolEffectJournal(sessions)
    intent = ledger.begin_intent(
        run_id="run-projection",
        owner="worker",
        claim_owner="projection-invocation",
        tool_call_id="projection-call",
        tool_name="Read",
        args={"path": "/workspace/a.txt"},
        recovery_policy="replay_safe",
    )
    ledger.commit_result(
        intent.effect_id,
        run_owner="worker",
        claim_owner="projection-invocation",
        result={"ok": True},
    )

    from core.services import log_service

    monkeypatch.setattr(log_service, "SessionLocal", sessions)
    projection_secret = "projection-secret-token-123456"
    log_service._write_tool_call_sync(
        {
            "run_id": "run-projection",
            "chat_id": "chat-1",
            "user_id": "user-1",
            "tool_name": "Read",
            "tool_call_id": "projection-call",
            "tool_args": {
                "path": "/workspace/a.txt",
                "command": f"curl -H 'Authorization: Bearer {projection_secret}' https://x",
                "url": f"https://example.test/?token={projection_secret}",
                "callback": f"https://example.test/cb?password={projection_secret}",
            },
            "tool_result": "contents",
            "status": "success",
        }
    )
    with sessions() as db:
        projection = db.query(ToolCallLog).one()
        assert projection.effect_id == intent.effect_id
        assert projection.id == intent.intent.result_id
        assert projection.tool_result == "contents"
        assert projection_secret not in json.dumps(projection.tool_args)
        assert projection.tool_args["command"]["_withheld"] is True
        assert "[REDACTED]" in projection.tool_args["url"]
        assert "[REDACTED]" in projection.tool_args["callback"]

    log_service._write_tool_call_sync(
        {
            "run_id": "run-projection",
            "chat_id": "chat-1",
            "user_id": "user-1",
            "tool_name": "Read",
            "tool_call_id": "new-provider-call-after-recovery",
            "effect_id": intent.effect_id,
            "result_id": intent.intent.result_id,
            "tool_args": {"path": "/workspace/a.txt"},
            "tool_result": "replayed contents",
            "status": "success",
        }
    )
    with sessions() as db:
        assert db.query(ToolCallLog).count() == 1
        projection = db.query(ToolCallLog).one()
        assert projection.id == intent.intent.result_id
        assert projection.tool_call_id == "new-provider-call-after-recovery"
        assert projection.tool_result == "replayed contents"
