from tests.orchestration.tool_effect_support import (
    log_records,
    asyncio,
    json,
    ThreadPoolExecutor,
    datetime,
    timedelta,
    timezone,
    Barrier,
    SimpleNamespace,
    pytest,
    Agent,
    ReActConfig,
    TextBlock,
    ToolCallBlock,
    ToolResultState,
    UserMsg,
    ChatResponse,
    ChatUsage,
    PermissionContext,
    PermissionMode,
    FunctionTool,
    Toolkit,
    ToolChunk,
    ToolResponse,
    Base,
    ChatRun,
    ChatSession,
    ToolCallLog,
    ToolEffectLease,
    ToolEffectLedger,
    ToolEffectReceipt,
    ScheduledTask,
    AgentRuntimeState,
    make_adapter,
    durable_adapter,
    _plain_replay_args,
    reconcile_scheduled_task_intent,
    RunJournal,
    DEFAULT_TOOL_RECOVERY_REGISTRY,
    ToolRecoveryRegistry,
    ToolEffectError,
    ToolIdempotencyConflict,
    ToolIntentCommitError,
    ToolOutcomeUnknown,
    ToolEffectGateway,
    ToolEffectJournal,
    ToolIntent,
    recover_incomplete_tool_effects,
    create_engine,
    sessionmaker,
    QueuePool,
    MutableClock,
    effect_env,
)

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
