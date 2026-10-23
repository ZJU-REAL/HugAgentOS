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
