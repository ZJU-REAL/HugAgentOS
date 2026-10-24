"""The state machine every side-effecting tool call goes through."""
from __future__ import annotations
from core.harness import tool_effect_context
from core.runs.tool_effects.journal import ToolEffectJournal
import asyncio
import inspect
import uuid
from contextlib import suppress
from typing import Any, Awaitable, Callable, Mapping, Optional
from core.runs.tool_effects import errors
from core.runs.tool_effects.registry import ToolRecoveryRegistry
from core.runs.tool_effects import types

class ToolEffectGateway:
    """The single execution state machine used by production middleware and tests."""

    def __init__(
        self,
        journal: ToolEffectJournal,
        registry: ToolRecoveryRegistry,
        *,
        poll_interval: float = 0.05,
        lease_seconds: int = 300,
    ) -> None:
        self.journal = journal
        self.registry = registry
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds

    @staticmethod
    async def _call(handler, intent: types.ToolIntent):  # noqa: ANN001, ANN205
        value = handler(intent) if handler is not None else None
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _reconciliation(value: Any) -> types.ReconciliationResult:
        if isinstance(value, types.ReconciliationResult):
            return value
        return (
            types.ReconciliationResult.applied(value)
            if value is not None
            else types.ReconciliationResult.unknown()
        )

    def _start_heartbeat(
        self,
        effect_id: str,
        *,
        run_owner: str,
        claim_owner: str,
    ) -> tuple[asyncio.Task, asyncio.Event]:
        lost = asyncio.Event()
        interval = max(0.05, min(30.0, self.lease_seconds / 3))

        async def _beat() -> None:
            while True:
                await asyncio.sleep(interval)
                try:
                    ok = await asyncio.to_thread(
                        self.journal.renew_invocation_claim,
                        effect_id,
                        run_owner=run_owner,
                        claim_owner=claim_owner,
                        lease_seconds=self.lease_seconds,
                    )
                except Exception:  # noqa: BLE001 - lost heartbeat means lost ownership
                    ok = False
                if not ok:
                    lost.set()
                    return

        return asyncio.create_task(_beat()), lost

    @staticmethod
    async def _stop_heartbeat(task: asyncio.Task) -> None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def execute_outcome(
        self,
        *,
        run_id: str,
        owner: str,
        tool_call_id: str,
        tool_name: str,
        args: Mapping[str, Any],
        invoke: Callable[[], Awaitable[Any] | Any],
        idempotency_key: Optional[str] = None,
        classify_failure: Optional[Callable[[Any], bool]] = None,
    ) -> types.ToolExecutionOutcome:
        claim_owner = f"tool:{uuid.uuid4().hex}"
        policy = self.registry.resolve(tool_name)
        while True:
            decision = await asyncio.to_thread(
                self.journal.begin_intent,
                run_id=run_id,
                owner=owner,
                claim_owner=claim_owner,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                args=args,
                recovery_policy=policy.policy,
                idempotency_key=idempotency_key,
                lease_seconds=self.lease_seconds,
            )
            if decision.action == "known":
                return types.ToolExecutionOutcome(
                    decision.result,
                    decision.intent.effect_id,
                    decision.intent.result_id,
                    decision.result_state,
                    False,
                )
            if decision.action == "unknown":
                raise errors.ToolOutcomeUnknown(decision.effect_id)
            if decision.action == "wait":
                await asyncio.sleep(self.poll_interval)
                terminal = await asyncio.to_thread(
                    self.journal.terminal_decision, decision.effect_id
                )
                if terminal is None:
                    continue
                if terminal.action == "unknown":
                    raise errors.ToolOutcomeUnknown(decision.effect_id)
                return types.ToolExecutionOutcome(
                    terminal.result,
                    terminal.intent.effect_id,
                    terminal.intent.result_id,
                    terminal.result_state,
                    False,
                )
            if decision.action not in ("execute", "reconcile"):
                raise errors.ToolEffectError(
                    f"unsupported tool intent action: {decision.action}"
                )

            await asyncio.to_thread(
                self.journal.acquire_invocation_claim,
                decision.effect_id,
                run_owner=owner,
                claim_owner=claim_owner,
                lease_seconds=self.lease_seconds,
            )
            heartbeat, claim_lost = self._start_heartbeat(
                decision.effect_id,
                run_owner=owner,
                claim_owner=claim_owner,
            )
            token = tool_effect_context.CURRENT_TOOL_EFFECT.set(
                tool_effect_context.ToolInvocationContext(
                    decision.intent.effect_id,
                    decision.intent.result_id,
                    decision.intent.idempotency_key,
                    decision.intent.run_id,
                    claim_owner,
                )
            )
            try:
                if decision.action == "reconcile":
                    if policy.reconciler is None:
                        self.journal.mark_unknown(
                            decision.effect_id,
                            owner=owner,
                            claim_owner=claim_owner,
                            reason="reconcile policy has no reconciliation adapter",
                        )
                        raise errors.ToolOutcomeUnknown(decision.effect_id)
                    reconciliation = self._reconciliation(
                        await self._call(policy.reconciler, decision.intent)
                    )
                    if claim_lost.is_set():
                        raise errors.ToolEffectLeaseLost(decision.effect_id)
                    if reconciliation.outcome == "applied":
                        result = await asyncio.to_thread(
                            self.journal.commit_result,
                            decision.effect_id,
                            run_owner=owner,
                            claim_owner=claim_owner,
                            result=reconciliation.result,
                            reconciled=True,
                        )
                        return types.ToolExecutionOutcome(
                            result,
                            decision.intent.effect_id,
                            decision.intent.result_id,
                            "success",
                            False,
                        )
                    if reconciliation.outcome != "not_applied":
                        self.journal.mark_unknown(
                            decision.effect_id,
                            owner=owner,
                            claim_owner=claim_owner,
                            reason="reconciliation could not determine external outcome",
                        )
                        raise errors.ToolOutcomeUnknown(decision.effect_id)
                value = invoke()
                result = await value if inspect.isawaitable(value) else value
                if claim_lost.is_set():
                    raise errors.ToolEffectLeaseLost(decision.effect_id)
                failed = (
                    bool(classify_failure(result))
                    if classify_failure is not None
                    else False
                )
                # A returned error payload is still a definitive tool outcome.
                # Persist it and give the original error back to the model so
                # it can correct the request or explain an authorization issue.
                # ToolOutcomeUnknown is reserved for invocations that never
                # produced a result at all (exceptions, disconnects, crashes).
                result = await asyncio.to_thread(
                    self.journal.commit_result,
                    decision.effect_id,
                    run_owner=owner,
                    claim_owner=claim_owner,
                    result=result,
                    failed=failed,
                )
                return types.ToolExecutionOutcome(
                    result,
                    decision.intent.effect_id,
                    decision.intent.result_id,
                    "error" if failed else "success",
                    True,
                )
            except asyncio.CancelledError:
                with suppress(Exception):
                    self.journal.abandon_invocation_claim(
                        decision.effect_id,
                        run_owner=owner,
                        claim_owner=claim_owner,
                    )
                raise
            except errors.ToolOutcomeUnknown:
                with suppress(Exception):
                    self.journal.abandon_invocation_claim(
                        decision.effect_id,
                        run_owner=owner,
                        claim_owner=claim_owner,
                    )
                raise
            except Exception as exc:
                with suppress(Exception):
                    self.journal.abandon_invocation_claim(
                        decision.effect_id,
                        run_owner=owner,
                        claim_owner=claim_owner,
                    )
                raise errors.ToolOutcomeUnknown(decision.effect_id) from exc
            finally:
                await self._stop_heartbeat(heartbeat)
                tool_effect_context.CURRENT_TOOL_EFFECT.reset(token)

    async def execute(self, **kwargs) -> Any:  # noqa: ANN003
        return (await self.execute_outcome(**kwargs)).result
