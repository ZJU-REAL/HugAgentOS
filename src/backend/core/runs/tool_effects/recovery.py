"""Recover tool effects left incomplete by a lost process."""
from __future__ import annotations
from core.runs.tool_effects.journal import ToolEffectJournal
import asyncio
import time
import uuid
from contextlib import suppress
from typing import Optional
import logging
from core.runs.tool_effects import errors
from core.runs.tool_effects import gateway
from core.runs.tool_effects.registry import DEFAULT_TOOL_RECOVERY_REGISTRY, ToolRecoveryRegistry
from core.runs.tool_effects import types

logger = logging.getLogger(__name__)

async def recover_incomplete_tool_effects(
    *,
    journal: Optional[ToolEffectJournal] = None,
    registry: Optional[ToolRecoveryRegistry] = None,
    lease_seconds: int = 120,
    heartbeat_interval: Optional[float] = None,
) -> list[types.RecoverySweepDecision]:
    """Recover each orphan independently; one broken adapter cannot starve later work."""

    journal = journal or ToolEffectJournal()
    registry = registry or DEFAULT_TOOL_RECOVERY_REGISTRY
    from core.harness.usage import UsageAttempt, attempt_status_for_exception, record_usage_safely
    from core.harness.ledger import HarnessUsageLedger

    usage_ledger = HarnessUsageLedger(journal._sessions)

    async def _recorded_recovery_call(handler, intent, phase):  # noqa: ANN001
        started = time.monotonic()
        status = "failed"
        try:
            result = await gateway.ToolEffectGateway._call(handler, intent)
            status = "success"
            return result
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            status = attempt_status_for_exception(exc)
            raise
        finally:
            try:
                await record_usage_safely(
                    usage_ledger,
                    UsageAttempt(
                        run_id=intent.run_id,
                        kind="tool",
                        operation_name=intent.tool_name,
                        effect_id=intent.effect_id,
                        status=status,
                        latency_ms=int((time.monotonic() - started) * 1_000),
                        metadata={"recovery_phase": phase},
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.debug("tool recovery usage persistence failed", exc_info=True)

    decisions: list[types.RecoverySweepDecision] = []
    for effect_id in await asyncio.to_thread(journal.pending_effect_ids):
        terminal_intent = journal.settle_terminal_run_intent(effect_id)
        if terminal_intent is not None:
            decisions.append(
                types.RecoverySweepDecision(
                    effect_id, terminal_intent.run_id, "terminal_unknown"
                )
            )
            continue
        recovery_owner = f"tool-recovery:{uuid.uuid4().hex}"
        intent = journal.claim_recovery_intent(
            effect_id,
            recovery_owner=recovery_owner,
            lease_seconds=lease_seconds,
        )
        if intent is None:
            continue
        policy = registry.resolve(intent.tool_name)
        claim_lost = asyncio.Event()

        async def _heartbeat() -> None:
            interval = heartbeat_interval
            if interval is None:
                interval = max(0.05, min(30.0, lease_seconds / 3))
            while True:
                await asyncio.sleep(interval)
                try:
                    ok = await asyncio.to_thread(
                        journal.renew_recovery_claim,
                        effect_id,
                        recovery_owner=recovery_owner,
                        lease_seconds=lease_seconds,
                    )
                except Exception:  # noqa: BLE001 - lost heartbeat means lost ownership
                    ok = False
                if not ok:
                    claim_lost.set()
                    return

        heartbeat = asyncio.create_task(_heartbeat())
        try:
            if intent.recovery_policy == "never_replay":
                journal.mark_unknown(
                    effect_id,
                    owner=recovery_owner,
                    claim_owner=recovery_owner,
                    reason="non-replayable tool intent has no committed result",
                )
                decisions.append(
                    types.RecoverySweepDecision(effect_id, intent.run_id, "needs_attention")
                )
                continue
            if intent.recovery_policy == "replay_safe":
                if policy.replayer is None:
                    raise errors.ToolEffectError("replay_safe policy has no replay adapter")
                result = await _recorded_recovery_call(
                    policy.replayer, intent, "replay"
                )
                if claim_lost.is_set():
                    raise errors.ToolEffectLeaseLost(effect_id)
                journal.commit_recovered_result(
                    effect_id, result, recovery_owner=recovery_owner
                )
                decisions.append(
                    types.RecoverySweepDecision(effect_id, intent.run_id, "replayed")
                )
                continue

            if policy.reconciler is None:
                raise errors.ToolEffectError("reconcile policy has no reconciliation adapter")
            reconciliation = gateway.ToolEffectGateway._reconciliation(
                await _recorded_recovery_call(policy.reconciler, intent, "reconcile")
            )
            if claim_lost.is_set():
                raise errors.ToolEffectLeaseLost(effect_id)
            if reconciliation.outcome == "applied":
                result = reconciliation.result
            elif (
                reconciliation.outcome == "not_applied" and policy.replayer is not None
            ):
                result = await _recorded_recovery_call(
                    policy.replayer, intent, "replay_after_reconcile"
                )
            else:
                journal.mark_unknown(
                    effect_id,
                    owner=recovery_owner,
                    claim_owner=recovery_owner,
                    reason="reconciliation could not determine external outcome",
                )
                decisions.append(
                    types.RecoverySweepDecision(effect_id, intent.run_id, "needs_attention")
                )
                continue
            if claim_lost.is_set():
                raise errors.ToolEffectLeaseLost(effect_id)
            journal.commit_recovered_result(
                effect_id, result, recovery_owner=recovery_owner
            )
            decisions.append(
                types.RecoverySweepDecision(effect_id, intent.run_id, "reconciled")
            )
        except Exception:  # noqa: BLE001 - release this claim and continue the sweep
            try:
                journal.release_recovery_claim(effect_id, recovery_owner=recovery_owner)
            except errors.ToolEffectError:
                pass
            decisions.append(types.RecoverySweepDecision(effect_id, intent.run_id, "retry"))
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
    return decisions
