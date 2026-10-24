"""Framework-neutral HookBus/EventSink contracts."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from core.harness.events import Event, EventSink
from core.harness.observation_writer import flush_observations
from core.harness.hooks import (
    Decision,
    DecisionKind,
    FailureMode,
    HookBus,
    HookPaused,
    HookSpec,
    HookStage,
    Invocation,
    find_hook_paused,
)
from core.harness.usage import (
    AttemptUsage,
    MemoryUsageRecorder,
    UsageAttempt,
    record_usage_safely,
)


def test_neutral_core_imports_without_loading_agentscope():
    backend = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(backend)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import core.harness; print('agentscope' in sys.modules)",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "False"


@pytest.mark.asyncio
async def test_hook_order_modify_and_reject_short_circuit():
    seen: list[tuple[str, tuple]] = []
    bus = HookBus()

    async def first(invocation):
        seen.append(("first", tuple(invocation.data["messages"])))
        return Decision.modify({"messages": ["changed"]})

    def second(invocation):
        seen.append(("second", tuple(invocation.data["messages"])))
        return Decision.reject("policy denied")

    def never(invocation):
        seen.append(("never", tuple(invocation.data["messages"])))

    bus.register(
        HookSpec(
            "first",
            HookStage.TRANSFORM_CONTEXT,
            first,
            priority=10,
            mutable_fields=frozenset({"messages"}),
        )
    )
    bus.register(HookSpec("second", HookStage.TRANSFORM_CONTEXT, second, priority=20))
    bus.register(HookSpec("never", HookStage.TRANSFORM_CONTEXT, never, priority=30))

    outcome = await bus.run(
        Invocation.create(
            run_id="run-hook",
            stage=HookStage.TRANSFORM_CONTEXT,
            operation_name="reply",
            data={"messages": ["original"], "context": {}},
        )
    )
    assert seen == [("first", ("original",)), ("second", ("changed",))]
    assert outcome.executed_hooks == ("first", "second")
    assert outcome.decision.kind == DecisionKind.REJECT
    assert tuple(outcome.invocation.data["messages"]) == ("changed",)


@pytest.mark.asyncio
async def test_pause_is_an_explicit_terminal_decision():
    bus = HookBus()
    bus.register(
        HookSpec(
            "approval",
            HookStage.BEFORE_TOOL,
            lambda invocation: Decision.pause("waiting for approval"),
        )
    )
    with pytest.raises(HookPaused, match="waiting for approval"):
        await bus.enforce(
            Invocation.create(
                run_id="run-pause",
                stage=HookStage.BEFORE_TOOL,
                operation_name="write",
                data={"tool_args": {}, "metadata": {}},
            )
        )

    nested = ExceptionGroup("task group", [ValueError("noise"), HookPaused("hold")])
    assert str(find_hook_paused(nested)) == "hold"


@pytest.mark.asyncio
async def test_timeout_policy_is_declared_fail_open_or_closed():
    async def slow(invocation):
        await asyncio.sleep(0.05)

    open_bus = HookBus()
    open_bus.register(
        HookSpec(
            "optional",
            HookStage.BEFORE_RUN,
            slow,
            timeout_ms=1,
            failure_mode=FailureMode.OPEN,
        )
    )
    open_outcome = await open_bus.run(
        Invocation.create(
            run_id="run-open",
            stage=HookStage.BEFORE_RUN,
            operation_name="reply",
            data={"metadata": {}},
        )
    )
    assert open_outcome.decision.kind == DecisionKind.CONTINUE

    closed_bus = HookBus()
    closed_bus.register(
        HookSpec(
            "required",
            HookStage.BEFORE_RUN,
            slow,
            timeout_ms=1,
            failure_mode=FailureMode.CLOSED,
        )
    )
    closed_outcome = await closed_bus.run(
        Invocation.create(
            run_id="run-closed",
            stage=HookStage.BEFORE_RUN,
            operation_name="reply",
            data={"metadata": {}},
        )
    )
    assert closed_outcome.decision.kind == DecisionKind.REJECT
    assert "timed out" in closed_outcome.decision.reason


@pytest.mark.asyncio
async def test_event_consumers_cannot_mutate_execution_payload():
    source = {"nested": {"value": 1}}
    attempts: list[str] = []
    sink = EventSink()

    def malicious(event):
        attempts.append("called")
        event.payload["nested"]["value"] = 99

    sink.subscribe(malicious)
    recorded = await sink.append(
        Event.create(
            run_id="run-event",
            event_type="observed",
            phase="before_model",
            payload=source,
        )
    )
    source["nested"]["value"] = 7
    assert attempts == ["called"]
    assert recorded.payload["nested"]["value"] == 1
    assert sink.events()[0].payload["nested"]["value"] == 1


@pytest.mark.asyncio
async def test_event_observers_are_bounded_liveness_channels():
    def stuck(_event):
        import time

        time.sleep(0.05)

    sink = EventSink(
        store=stuck,
        store_timeout_ms=1,
        subscriber_timeout_ms=1,
    )
    sink.subscribe(stuck)
    started = asyncio.get_running_loop().time()
    await sink.append(
        Event.create(
            run_id="run-events",
            event_type="bounded",
            phase="before_run",
        )
    )
    assert asyncio.get_running_loop().time() - started < 0.04


@pytest.mark.asyncio
async def test_usage_recorder_is_a_bounded_liveness_channel():
    def stuck(_attempt):
        import time

        time.sleep(0.05)

    started = asyncio.get_running_loop().time()
    assert (
        await record_usage_safely(
            type("Recorder", (), {"record_attempt": staticmethod(stuck)})(),
            UsageAttempt(
                run_id="run-usage-timeout",
                kind="model",
                operation_name="model",
                status="success",
                latency_ms=1,
            ),
            timeout_ms=1,
        )
        is None
    )
    assert asyncio.get_running_loop().time() - started < 0.04


@pytest.mark.asyncio
async def test_hook_external_usage_is_recorded_as_its_own_attempt():
    usage = MemoryUsageRecorder()
    bus = HookBus(usage_recorder=usage)
    bus.register(
        HookSpec(
            "safety_classifier",
            HookStage.BEFORE_MODEL,
            lambda invocation: Decision(
                usage=AttemptUsage(prompt_tokens=11, completion_tokens=3)
            ),
        )
    )
    await bus.run(
        Invocation.create(
            run_id="run-usage",
            stage=HookStage.BEFORE_MODEL,
            operation_name="model",
            data={"model": "m", "messages": [], "parameters": {}},
        )
    )
    assert flush_observations()
    rows = usage.attempts("run-usage")
    assert len(rows) == 1
    assert rows[0].kind == "hook"
    assert rows[0].operation_name == "safety_classifier"
    assert usage.aggregate("run-usage")["total_tokens"] == 14


@pytest.mark.asyncio
async def test_interactive_policy_owns_wait_budget_and_preserves_cancellation(monkeypatch):
    from core.llm.tool_permissions.hook import ToolPermissionHook
    from dataclasses import replace

    entered = asyncio.Event()
    approved = asyncio.Event()
    cancelled = asyncio.Event()

    async def approval(invocation):
        entered.set()
        try:
            await approved.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return Decision()

    async def short_watchdog(coro, timeout):
        coro.close()
        raise AssertionError("Human approval must own its wait budget")

    bus = HookBus()
    bus.register(replace(ToolPermissionHook(None).spec(), handler=approval))
    invocation = Invocation.create(run_id="approval", stage=HookStage.BEFORE_TOOL,
                                   operation_name="write", data={"tool_args": {}, "metadata": {}})
    monkeypatch.setattr(asyncio, "wait_for", short_watchdog)
    task = asyncio.create_task(bus.run(invocation))
    await entered.wait()
    await asyncio.sleep(0)
    assert not task.done()
    approved.set()
    assert (await task).decision.kind == DecisionKind.CONTINUE
    entered.clear()
    approved.clear()
    task = asyncio.create_task(bus.run(invocation))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_sync_handlers_run_inline_without_executor_or_timeout(monkeypatch):
    import threading

    async def saturated(*args, **kwargs):
        raise AssertionError("sync hooks must not wait for the default executor")

    loop_thread = threading.get_ident()
    seen = []

    def inline(invocation):
        seen.append(threading.get_ident())
        return Decision()

    bus = HookBus()
    bus.register(HookSpec("durable", HookStage.BEFORE_TOOL, inline, timeout_ms=1))
    monkeypatch.setattr(asyncio, "to_thread", saturated)
    outcome = await bus.run(
        Invocation.create(
            run_id="run-inline",
            stage=HookStage.BEFORE_TOOL,
            operation_name="write",
        )
    )
    assert outcome.decision.kind == DecisionKind.CONTINUE
    assert seen == [loop_thread]


@pytest.mark.asyncio
async def test_stage_writes_one_event_and_one_usage_batch():
    class BatchRecorder(MemoryUsageRecorder):
        def __init__(self):
            super().__init__()
            self.batches = []

        def record_attempts(self, attempts):
            self.batches.append(len(attempts))
            return super().record_attempts(attempts)

    usage = BatchRecorder()
    bus = HookBus(usage_recorder=usage)
    for name, priority in (("one", 10), ("two", 20), ("three", 30)):
        bus.register(HookSpec(name, HookStage.TRANSFORM_CONTEXT, lambda invocation: None,
                              priority=priority))
    await bus.run(
        Invocation.create(
            run_id="run-batch",
            stage=HookStage.TRANSFORM_CONTEXT,
            operation_name="agent_reasoning",
        )
    )
    events = bus.event_sink.events()
    assert [event.event_type for event in events] == ["hook_stage_finished"]
    assert flush_observations()
    assert [item["hook"] for item in events[0].payload["attempts"]] == ["one", "two", "three"]
    assert usage.batches == [3]
    assert [row.attempt_seq for row in usage.attempts("run-batch")] == [1, 2, 3]

    await bus.run(
        Invocation.create(
            run_id="run-batch",
            stage=HookStage.BEFORE_MODEL,
            operation_name="model",
        )
    )
    assert len(bus.event_sink.events()) == 1
    assert flush_observations()
    assert usage.batches == [3]


@pytest.mark.asyncio
async def test_cancelled_hook_still_flushes_the_stage_batch():
    usage = MemoryUsageRecorder()
    bus = HookBus(usage_recorder=usage)

    async def cancelled(invocation):
        raise asyncio.CancelledError

    bus.register(HookSpec("first", HookStage.BEFORE_TOOL, lambda invocation: None, priority=10))
    bus.register(HookSpec("cancel", HookStage.BEFORE_TOOL, cancelled, priority=20))
    bus.register(HookSpec("never", HookStage.BEFORE_TOOL, lambda invocation: None, priority=30))
    with pytest.raises(asyncio.CancelledError):
        await bus.run(
            Invocation.create(
                run_id="run-cancel",
                stage=HookStage.BEFORE_TOOL,
                operation_name="write",
            )
        )
    assert flush_observations()
    assert [(row.operation_name, row.status) for row in usage.attempts("run-cancel")] == [
        ("first", "success"),
        ("cancel", "cancelled"),
    ]
