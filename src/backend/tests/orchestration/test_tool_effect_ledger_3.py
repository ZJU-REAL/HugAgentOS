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
    from extensions.mcp_servers.automation_task_mcp import impl

    monkeypatch.setattr(engine_module, "SessionLocal", sessions)
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
    middleware = durable_adapter(sessions)
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
    middleware = durable_adapter(sessions)
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
    middleware = durable_adapter(sessions)
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
    middleware = durable_adapter(sessions)
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
    middleware = durable_adapter(sessions)
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
        from agentscope.formatter import OpenAIChatFormatter
        formatter = OpenAIChatFormatter()
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
        middlewares=[durable_adapter(sessions)],
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
