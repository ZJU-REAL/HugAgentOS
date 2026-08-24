"""Ordered, explicit and framework-neutral execution hooks."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any

from core.harness.events import Event, EventSink, freeze_value, thaw_value
from core.harness.usage import AttemptUsage, UsageAttempt, UsageRecorder


class HookStage(str, Enum):
    BEFORE_RUN = "before_run"
    TRANSFORM_CONTEXT = "transform_context"
    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    BEFORE_COMPACTION = "before_compaction"
    AFTER_COMPACTION = "after_compaction"
    BEFORE_FINISH = "before_finish"


class DecisionKind(str, Enum):
    CONTINUE = "continue"
    MODIFY = "modify"
    REJECT = "reject"
    PAUSE = "pause"


class FailureMode(str, Enum):
    OPEN = "fail_open"
    CLOSED = "fail_closed"


STAGE_MUTABLE_FIELDS: Mapping[HookStage, frozenset[str]] = MappingProxyType(
    {
        HookStage.BEFORE_RUN: frozenset(),
        HookStage.TRANSFORM_CONTEXT: frozenset({"messages", "context"}),
        # AgentScope reaches on_model_call only after HugAgentOS has assembled and
        # bound the execution manifest. Rewriting provider input here would make
        # the evidence bundle describe different inputs than the real request.
        HookStage.BEFORE_MODEL: frozenset(),
        HookStage.AFTER_MODEL: frozenset(),
        # AgentScope reaches on_acting after schema and permission checks. A
        # rewrite here would bypass both, so the call is observation-only.
        HookStage.BEFORE_TOOL: frozenset(),
        # Tool chunks may already be visible by this point, so results are
        # read-only here; policies that rewrite calls must act BEFORE_TOOL.
        HookStage.AFTER_TOOL: frozenset(),
        # Source history is read-only because the checkpoint watermark/hash
        # must continue to describe exactly the rows being covered.
        HookStage.BEFORE_COMPACTION: frozenset({"budget"}),
        HookStage.AFTER_COMPACTION: frozenset({"replacement"}),
        HookStage.BEFORE_FINISH: frozenset(),
    }
)


@dataclass(frozen=True)
class Invocation:
    run_id: str
    stage: HookStage
    operation_name: str
    data: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        stage: HookStage,
        operation_name: str,
        data: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Invocation:
        return cls(
            run_id=run_id,
            stage=stage,
            operation_name=operation_name,
            data=freeze_value(data or {}),
            metadata=freeze_value(metadata or {}),
        )

    def patched(self, patch: Mapping[str, Any]) -> Invocation:
        merged = thaw_value(self.data)
        merged.update(thaw_value(patch))
        return replace(self, data=freeze_value(merged))


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind = DecisionKind.CONTINUE
    patch: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    usage: AttemptUsage = field(default_factory=AttemptUsage)

    @classmethod
    def modify(
        cls,
        patch: Mapping[str, Any],
        *,
        usage: AttemptUsage | None = None,
    ) -> Decision:
        return cls(
            DecisionKind.MODIFY, freeze_value(patch), usage=usage or AttemptUsage()
        )

    @classmethod
    def reject(cls, reason: str) -> Decision:
        return cls(DecisionKind.REJECT, reason=reason)

    @classmethod
    def pause(cls, reason: str) -> Decision:
        return cls(DecisionKind.PAUSE, reason=reason)


HookHandler = Callable[[Invocation], Decision | Awaitable[Decision] | None]


@dataclass(frozen=True)
class HookSpec:
    name: str
    stage: HookStage
    handler: HookHandler
    priority: int = 100
    timeout_ms: int = 1_000
    failure_mode: FailureMode = FailureMode.CLOSED
    mutable_fields: frozenset[str] = field(default_factory=frozenset)


class HookContractError(RuntimeError):
    pass


class HookRejected(RuntimeError):
    pass


class HookPaused(RuntimeError):
    pass


def find_hook_paused(exc: BaseException) -> HookPaused | None:
    """Find a pause request even when TaskGroup wrapped it in an ExceptionGroup."""
    if isinstance(exc, HookPaused):
        return exc
    for nested in getattr(exc, "exceptions", ()):
        found = find_hook_paused(nested)
        if found is not None:
            return found
    return None


@dataclass(frozen=True)
class HookOutcome:
    invocation: Invocation
    decision: Decision
    executed_hooks: tuple[str, ...]


class HookBus:
    """Deterministic hook pipeline with declared mutation and failure policy."""

    def __init__(
        self,
        *,
        event_sink: EventSink | None = None,
        usage_recorder: UsageRecorder | None = None,
        usage_timeout_ms: int = 1_000,
    ) -> None:
        self.event_sink = event_sink or EventSink()
        self.usage_recorder = usage_recorder
        self.usage_timeout_ms = max(1, int(usage_timeout_ms))
        self._hooks: dict[HookStage, list[tuple[int, int, HookSpec]]] = {}
        self._registration_seq = 0

    def register(self, spec: HookSpec) -> None:
        allowed = STAGE_MUTABLE_FIELDS[spec.stage]
        if not spec.mutable_fields.issubset(allowed):
            invalid = sorted(spec.mutable_fields - allowed)
            raise HookContractError(
                f"hook {spec.name} declares fields outside "
                f"{spec.stage.value}: {invalid}"
            )
        self._registration_seq += 1
        bucket = self._hooks.setdefault(spec.stage, [])
        bucket.append((spec.priority, self._registration_seq, spec))
        bucket.sort(key=lambda item: (item[0], item[1]))

    async def _call(self, spec: HookSpec, invocation: Invocation) -> Decision:
        async def invoke() -> Decision:
            if inspect.iscoroutinefunction(spec.handler):
                result = await spec.handler(invocation)
            else:
                result = await asyncio.to_thread(spec.handler, invocation)
                if inspect.isawaitable(result):
                    result = await result
            if result is None:
                return Decision()
            if not isinstance(result, Decision):
                raise HookContractError(
                    f"hook {spec.name} returned {type(result).__name__}"
                )
            return result

        return await asyncio.wait_for(invoke(), timeout=max(1, spec.timeout_ms) / 1_000)

    async def run(self, invocation: Invocation) -> HookOutcome:
        current = invocation
        executed: list[str] = []
        terminal = Decision()
        await self.event_sink.append(
            Event.create(
                run_id=current.run_id,
                event_type="hook_stage_started",
                phase=current.stage.value,
                payload={"operation_name": current.operation_name},
            )
        )
        for _, _, spec in tuple(self._hooks.get(current.stage, ())):
            started = time.monotonic()
            status = "success"
            decision = Decision()
            cancellation: asyncio.CancelledError | None = None
            try:
                decision = await self._call(spec, current)
                if decision.kind == DecisionKind.MODIFY:
                    invalid = set(decision.patch) - set(spec.mutable_fields)
                    if invalid:
                        raise HookContractError(
                            f"hook {spec.name} modified undeclared fields: "
                            f"{sorted(invalid)}"
                        )
                    current = current.patched(decision.patch)
                elif decision.kind in {DecisionKind.REJECT, DecisionKind.PAUSE}:
                    terminal = decision
            except asyncio.CancelledError as exc:
                status = "cancelled"
                cancellation = exc
            except TimeoutError:
                status = "timeout"
                if spec.failure_mode == FailureMode.CLOSED:
                    terminal = Decision.reject(f"hook {spec.name} timed out")
            except Exception as exc:  # noqa: BLE001
                status = "failed"
                if spec.failure_mode == FailureMode.CLOSED:
                    terminal = Decision.reject(f"hook {spec.name} failed: {exc}")
            latency_ms = int((time.monotonic() - started) * 1_000)
            executed.append(spec.name)
            if self.usage_recorder is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            self.usage_recorder.record_attempt,
                            UsageAttempt(
                                run_id=current.run_id,
                                kind="hook",
                                operation_name=spec.name,
                                status=status,
                                latency_ms=latency_ms,
                                usage=decision.usage,
                                metadata={"stage": current.stage.value},
                            ),
                        ),
                        timeout=self.usage_timeout_ms / 1_000,
                    )
                except Exception:  # noqa: BLE001, S110
                    pass
            await self.event_sink.append(
                Event.create(
                    run_id=current.run_id,
                    event_type="hook_attempt_finished",
                    phase=current.stage.value,
                    payload={
                        "hook": spec.name,
                        "status": status,
                        "decision": terminal.kind.value
                        if terminal.kind != DecisionKind.CONTINUE
                        else decision.kind.value,
                        "latency_ms": latency_ms,
                    },
                )
            )
            if terminal.kind in {DecisionKind.REJECT, DecisionKind.PAUSE}:
                break
            if cancellation is not None:
                raise cancellation

        await self.event_sink.append(
            Event.create(
                run_id=current.run_id,
                event_type="hook_stage_finished",
                phase=current.stage.value,
                payload={
                    "decision": terminal.kind.value,
                    "executed_hooks": executed,
                },
            )
        )
        return HookOutcome(current, terminal, tuple(executed))

    async def enforce(self, invocation: Invocation) -> Invocation:
        outcome = await self.run(invocation)
        if outcome.decision.kind == DecisionKind.REJECT:
            raise HookRejected(outcome.decision.reason)
        if outcome.decision.kind == DecisionKind.PAUSE:
            raise HookPaused(outcome.decision.reason)
        return outcome.invocation
