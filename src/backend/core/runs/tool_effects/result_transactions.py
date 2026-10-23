"""Commit tool results or mark their outcome unknown."""
from __future__ import annotations
from typing import Any, Optional
from core.db.models import ChatRun, ToolEffectLease, ToolEffectLedger
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, aliased
from core.runs.tool_effects import errors
from core.runs.tool_effects import types
from core.infra.time import as_utc

def append_event(
    self,
    db: Session,
    run: ChatRun,
    intent: ToolEffectLedger,
    *,
    owner: str,
    event_type: str,
    operation_type: str,
    phase: str,
    safety: str,
    result: Any = None,
    error: Optional[str] = None,
    terminal: bool = False,
) -> None:
    receipt = self._runs._append_locked(
        db,
        run,
        owner=owner,
        operation_type=operation_type,
        phase=phase,
        safety=safety,
        payload={"effect_id": intent.effect_id, "result_id": intent.result_id},
    )
    db.add(
        ToolEffectLedger(
            effect_id=intent.effect_id,
            result_id=intent.result_id,
            run_id=intent.run_id,
            operation_seq=receipt.operation_seq,
            event_type=event_type,
            tool_name=intent.tool_name,
            tool_call_id=intent.tool_call_id,
            args_hash=intent.args_hash,
            redacted_args=intent.redacted_args,
            recovery_policy=intent.recovery_policy,
            result_payload=result,
            error_message=error,
            terminal_effect_id=intent.effect_id if terminal else None,
        )
    )


def mark_unknown_locked(
    self,
    db: Session,
    run: ChatRun,
    intent: ToolEffectLedger,
    lease: ToolEffectLease,
    *,
    owner: str,
    claim_owner: str,
    reason: str,
) -> None:
    if self._terminal_row(db, intent.effect_id) is not None:
        return
    if lease.run_owner != owner or lease.claim_owner != claim_owner:
        raise errors.ToolEffectLeaseLost(intent.effect_id)
    self._append_event(
        db,
        run,
        intent,
        owner=owner,
        event_type="unknown_outcome",
        operation_type="tool_outcome_unknown",
        phase="needs_attention",
        safety="manual_review",
        error=reason,
        terminal=True,
    )
    run.status = "needs_attention"
    run.failure_reason = reason
    run.lease_owner = None
    run.lease_expires_at = None
    run.updated_at = self._clock()
    lease.claim_owner = None
    lease.lease_expires_at = None
    lease.updated_at = self._clock()


def commit_result_locked(
    self,
    db: Session,
    intent: ToolEffectLedger,
    lease: ToolEffectLease,
    *,
    owner: str,
    result: Any,
    failed: bool,
    reconciled: bool,
) -> Any:
    terminal = self._terminal_row(db, intent.effect_id)
    if terminal is not None:
        if terminal.event_type == "unknown_outcome":
            raise errors.ToolOutcomeUnknown(intent.effect_id)
        return terminal.result_payload
    if lease.run_owner != owner:
        raise errors.ToolEffectLeaseLost(intent.effect_id)
    run = self._runs._locked_owned_run(db, intent.run_id, owner)
    self._append_event(
        db,
        run,
        intent,
        owner=owner,
        event_type="failure" if failed else "result",
        operation_type="tool_failure_committed"
        if failed
        else "tool_result_committed",
        phase="tool_result_committed",
        safety="reconciled" if reconciled else intent.recovery_policy,
        result=result,
        error=str(result)[:4000] if failed else None,
        terminal=True,
    )
    lease.claim_owner = None
    lease.lease_expires_at = None
    lease.updated_at = self._clock()
    run.updated_at = self._clock()
    return result


def commit_result(
    self,
    effect_id: str,
    *,
    run_owner: str,
    claim_owner: str,
    result: Any,
    failed: bool = False,
    reconciled: bool = False,
    _conflict_retries: int = 4,
) -> Any:
    with self._sessions() as db:
        intent = (
            db.query(ToolEffectLedger)
            .filter(
                ToolEffectLedger.effect_id == effect_id,
                ToolEffectLedger.event_type == "intent",
            )
            .one()
        )
        terminal = self._terminal_row(db, effect_id)
        if terminal is not None:
            if terminal.event_type == "unknown_outcome":
                raise errors.ToolOutcomeUnknown(effect_id)
            return terminal.result_payload
        lease = (
            db.query(ToolEffectLease)
            .filter(ToolEffectLease.effect_id == effect_id)
            .with_for_update()
            .one()
        )
        now = as_utc(self._clock())
        lease_expiry = as_utc(lease.lease_expires_at)
        if (
            lease.run_owner != run_owner
            or lease.claim_owner != claim_owner
            or lease_expiry is None
            or now is None
            or lease_expiry <= now
        ):
            # A concurrent retry can observe the lease only after the
            # winning commit cleared its claim. Re-check the durable
            # terminal decision before reporting lease loss: identical
            # result commits are idempotent even when the first pre-lock
            # terminal read raced with the winner.
            terminal = self._terminal_row(db, effect_id)
            if terminal is not None:
                if terminal.event_type == "unknown_outcome":
                    raise errors.ToolOutcomeUnknown(effect_id)
                return terminal.result_payload
            raise errors.ToolEffectLeaseLost(effect_id)
        value = self._commit_result_locked(
            db,
            intent,
            lease,
            owner=run_owner,
            result=result,
            failed=failed,
            reconciled=reconciled,
        )
        try:
            db.commit()
        except (IntegrityError, OperationalError) as exc:
            db.rollback()
            terminal = self.terminal_decision(effect_id)
            if terminal is not None:
                if terminal.action == "unknown":
                    raise errors.ToolOutcomeUnknown(effect_id)
                return terminal.result
            if _conflict_retries > 0:
                return self.commit_result(
                    effect_id,
                    run_owner=run_owner,
                    claim_owner=claim_owner,
                    result=result,
                    failed=failed,
                    reconciled=reconciled,
                    _conflict_retries=_conflict_retries - 1,
                )
            raise errors.ToolEffectError(str(exc)) from exc
        return value


def mark_unknown(
    self,
    effect_id: str,
    *,
    owner: str,
    claim_owner: str,
    reason: str,
) -> None:
    with self._sessions() as db:
        lease = (
            db.query(ToolEffectLease)
            .filter(ToolEffectLease.effect_id == effect_id)
            .with_for_update()
            .one()
        )
        intent = (
            db.query(ToolEffectLedger)
            .filter(
                ToolEffectLedger.effect_id == effect_id,
                ToolEffectLedger.event_type == "intent",
            )
            .one()
        )
        run = self._runs._locked_owned_run(db, intent.run_id, owner)
        self._mark_unknown_locked(
            db,
            run,
            intent,
            lease,
            owner=owner,
            claim_owner=claim_owner,
            reason=reason,
        )
        db.commit()


def terminal_decision(self, effect_id: str) -> Optional[types.IntentDecision]:
    with self._sessions() as db:
        intent = (
            db.query(ToolEffectLedger)
            .filter(
                ToolEffectLedger.effect_id == effect_id,
                ToolEffectLedger.event_type == "intent",
            )
            .one_or_none()
        )
        if intent is None:
            return None
        terminal = self._terminal_row(db, effect_id)
        return (
            self._decision_from_terminal(self._intent_from_row(intent), terminal)
            if terminal is not None
            else None
        )


def pending_effect_ids(self) -> list[str]:
    with self._sessions() as db:
        terminal = aliased(ToolEffectLedger)
        rows = (
            db.query(ToolEffectLedger.effect_id)
            .filter(
                ToolEffectLedger.event_type == "intent",
                ~db.query(terminal.event_id)
                .filter(terminal.terminal_effect_id == ToolEffectLedger.effect_id)
                .exists(),
            )
            .order_by(ToolEffectLedger.created_at, ToolEffectLedger.event_id)
            .all()
        )
        return [row.effect_id for row in rows]
