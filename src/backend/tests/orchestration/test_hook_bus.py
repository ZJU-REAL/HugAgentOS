"""Framework-neutral HookBus/EventSink contracts."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from core.harness.events import Event, EventSink
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
    rows = usage.attempts("run-usage")
    assert len(rows) == 1
    assert rows[0].kind == "hook"
    assert rows[0].operation_name == "safety_classifier"
    assert usage.aggregate("run-usage")["total_tokens"] == 14
