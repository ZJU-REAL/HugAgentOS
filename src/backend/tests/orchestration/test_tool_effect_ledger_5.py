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

def test_prune_settled_eventually_reclaims_needs_attention_runs(effect_env):
    """needs_attention 保留得更久，但不是永远。

    原先它被整个排除在清理之外，等于永不回收：生产库里每一条超出保留期的
    journal 记录——四万条——都属于 needs_attention 的 run，被永久钉住，还要被
    每五分钟一轮的清理反复全表扫。保留期该是"更长"，不是"无限"。
    """
    from core.db.models import HarnessEventLog
    from core.runs.tool_effects.journal_constants import ATTENTION_RETENTION_FACTOR as _ATTENTION_RETENTION_FACTOR

    sessions, make_run = effect_env
    clock = MutableClock()
    ledger = ToolEffectJournal(sessions, clock=clock)
    make_run("run-attention")
    intent = ledger.begin_intent(
        run_id="run-attention",
        owner="worker",
        claim_owner="invocation",
        tool_call_id="call-attention",
        tool_name="write",
        args={"path": "/tmp/a"},
        recovery_policy="never_replay",
        idempotency_key="prune-attention",
    )
    ledger.commit_result(
        intent.effect_id,
        run_owner="worker",
        claim_owner="invocation",
        result={"ok": True},
    )
    with sessions() as db:
        run = db.get(ChatRun, "run-attention")
        run.status = "needs_attention"
        # recovery_snapshot 是整轮对话的快照，库里最大的单个字段；它跟 journal
        # 走同一套分档，不能因为状态是 needs_attention 就永久留着
        run.completed_at = clock.now
        db.add(
            HarnessEventLog(
                event_id="hev-attention",
                run_id="run-attention",
                event_seq=1,
                event_type="tool_result",
                phase="post",
                payload={},
            )
        )
        db.commit()

    # 保留期显式传入，两次推进都从它算——否则 prune_settled 的默认值一改，
    # 这里的天数就悄悄测不到边界了
    retention_days = 7

    # 刚过普通终态的保留期：记录必须还在
    clock.advance((retention_days + 1) * 24 * 3600)
    ledger.prune_settled(retention_days=retention_days)
    with sessions() as db:
        assert (
            db.query(ToolEffectLedger).filter(ToolEffectLedger.run_id == "run-attention").count()
            > 0
        )
        assert (
            db.query(HarnessEventLog).filter(HarnessEventLog.run_id == "run-attention").count() == 1
        )
        assert db.get(ChatRun, "run-attention").recovery_snapshot is not None

    # 过了加长保留期：回收，不能无限期占着库
    clock.advance(retention_days * _ATTENTION_RETENTION_FACTOR * 24 * 3600)
    ledger.prune_settled(retention_days=retention_days)
    with sessions() as db:
        assert (
            db.query(ToolEffectLedger).filter(ToolEffectLedger.run_id == "run-attention").count()
            == 0
        )
        assert (
            db.query(HarnessEventLog).filter(HarnessEventLog.run_id == "run-attention").count() == 0
        )
        assert db.get(ChatRun, "run-attention").recovery_snapshot is None
