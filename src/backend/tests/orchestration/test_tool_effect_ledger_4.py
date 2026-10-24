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
        from agentscope.formatter import OpenAIChatFormatter
        formatter = OpenAIChatFormatter()
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
        toolkit=Toolkit(tools=[FunctionTool(blocking_probe, is_concurrency_safe=False)]),
        middlewares=[durable_adapter(sessions)],
        state=AgentRuntimeState(
            run_id="run-public-cancel",
            journal_owner="worker",
            permission_context=PermissionContext(mode=PermissionMode.BYPASS),
        ),
        react_config=ReActConfig(max_iters=2, interruption_raise_cancelled_error=True),
    )
    reply_task = asyncio.create_task(agent.reply(UserMsg(name="user", content="start")))
    await entered.wait()
    assert RunJournal(sessions).cancel("run-public-cancel", reason="test cancellation")
    reply_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(reply_task, timeout=5)
    release.set()
    await asyncio.sleep(0)
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

    from core.harness import log_writer as log_service

    monkeypatch.setattr(log_records, "SessionLocal", sessions)
    projection_secret = "projection-secret-token-123456"
    log_records.write_tool_call_sync(
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

    log_records.write_tool_call_sync(
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


def test_prune_settled_drops_only_old_rows_of_settled_runs(effect_env):
    sessions, make_run = effect_env
    clock = MutableClock()
    ledger = ToolEffectJournal(sessions, clock=clock)
    make_run("run-old")
    make_run("run-live", chat_id="chat-2")
    old = ledger.begin_intent(
        run_id="run-old",
        owner="worker",
        claim_owner="invocation",
        tool_call_id="call-old",
        tool_name="write",
        args={"path": "/tmp/a"},
        recovery_policy="never_replay",
        idempotency_key="prune-old",
    )
    ledger.commit_result(
        old.effect_id,
        run_owner="worker",
        claim_owner="invocation",
        result={"ok": True},
    )
    live = ledger.begin_intent(
        run_id="run-live",
        owner="worker",
        claim_owner="invocation",
        tool_call_id="call-live",
        tool_name="write",
        args={"path": "/tmp/b"},
        recovery_policy="never_replay",
        idempotency_key="prune-live",
    )
    from core.db.models import ChatRunOperation, HarnessEventLog, HarnessUsageAttempt

    with sessions() as db:
        run = db.get(ChatRun, "run-old")
        run.status = "completed"
        db.add(
            HarnessUsageAttempt(
                attempt_id="hat-old",
                run_id="run-old",
                attempt_seq=1,
                kind="tool",
                operation_name="write",
                status="success",
            )
        )
        db.add(
            HarnessEventLog(
                event_id="hev-old",
                run_id="run-old",
                event_seq=1,
                event_type="tool_result",
                phase="post",
                payload={},
            )
        )
        db.commit()
    clock.advance(8 * 24 * 3600)
    assert ledger.prune_settled() >= 4
    with sessions() as db:
        for model in (
            ToolEffectLedger,
            HarnessUsageAttempt,
            HarnessEventLog,
            ChatRunOperation,
        ):
            assert db.query(model).filter(model.run_id == "run-old").count() == 0
        assert (
            db.query(ToolEffectLedger)
            .filter(ToolEffectLedger.run_id == "run-live")
            .count()
            == 1
        )
        assert (
            db.query(ChatRunOperation)
            .filter(ChatRunOperation.run_id == "run-live")
            .count()
            > 0
        )
    assert ledger.pending_effect_ids() == [live.effect_id]
