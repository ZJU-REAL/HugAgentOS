"""Synchronous transaction boundary behind the tool effect gateway."""
from __future__ import annotations
from core.runs.tool_effects import intent_transactions
from core.runs.tool_effects import result_transactions
from core.runs.tool_effects import invocation_claims
from core.runs.tool_effects import retention
from core.runs.tool_effects import recovery_claims
from datetime import datetime
from typing import Any, Callable, Mapping, Optional
from core.db.engine import SessionLocal
from core.db.models import ChatRun, ToolEffectLease, ToolEffectLedger
from core.runs.journal import RunJournal
from sqlalchemy.orm import Session, sessionmaker
from core.runs.tool_effects import types
from core.infra.time import utc_now

class ToolEffectJournal:
    """Small synchronous transaction boundary behind the async gateway."""

    def __init__(
        self,
        session_factory: sessionmaker = SessionLocal,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock
        self._runs = RunJournal(session_factory, clock=clock)

    @staticmethod
    def _intent_from_row(row: ToolEffectLedger) -> types.ToolIntent:
        return intent_transactions.intent_from_row(row)

    @staticmethod
    def _terminal_row(db: Session, effect_id: str) -> Optional[ToolEffectLedger]:
        return intent_transactions.terminal_row(db, effect_id)

    @staticmethod
    def _decision_from_terminal(
        intent: types.ToolIntent, terminal: ToolEffectLedger
    ) -> types.IntentDecision:
        return intent_transactions.decision_from_terminal(intent, terminal)

    @staticmethod
    def _validate_identity(
        row: ToolEffectLedger, *, tool_name: str, args_hash: str
    ) -> None:
        return intent_transactions.validate_identity(row, tool_name=tool_name, args_hash=args_hash)

    def _append_event(
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
        return result_transactions.append_event(self, db, run, intent, owner=owner, event_type=event_type, operation_type=operation_type, phase=phase, safety=safety, result=result, error=error, terminal=terminal)

    def _mark_unknown_locked(
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
        return result_transactions.mark_unknown_locked(self, db, run, intent, lease, owner=owner, claim_owner=claim_owner, reason=reason)

    def _existing_decision(
        self,
        *,
        key: str,
        caller_run_id: str,
        run_owner: str,
        claim_owner: str,
        tool_name: str,
        args_hash: str,
        lease_seconds: int,
    ) -> types.IntentDecision:
        return intent_transactions.existing_decision(self, key=key, caller_run_id=caller_run_id, run_owner=run_owner, claim_owner=claim_owner, tool_name=tool_name, args_hash=args_hash, lease_seconds=lease_seconds)

    def begin_intent(
        self,
        *,
        run_id: str,
        owner: str,
        claim_owner: str,
        tool_call_id: str,
        tool_name: str,
        args: Mapping[str, Any],
        recovery_policy: str,
        idempotency_key: Optional[str] = None,
        lease_seconds: int = 300,
        _conflict_retries: int = 4,
    ) -> types.IntentDecision:
        return intent_transactions.begin_intent(self, run_id=run_id, owner=owner, claim_owner=claim_owner, tool_call_id=tool_call_id, tool_name=tool_name, args=args, recovery_policy=recovery_policy, idempotency_key=idempotency_key, lease_seconds=lease_seconds, _conflict_retries=_conflict_retries)

    def acquire_invocation_claim(
        self,
        effect_id: str,
        *,
        run_owner: str,
        claim_owner: str,
        lease_seconds: int = 300,
    ) -> None:
        return invocation_claims.acquire_invocation_claim(self, effect_id, run_owner=run_owner, claim_owner=claim_owner, lease_seconds=lease_seconds)

    def renew_invocation_claim(
        self,
        effect_id: str,
        *,
        run_owner: str,
        claim_owner: str,
        lease_seconds: int,
    ) -> bool:
        return invocation_claims.renew_invocation_claim(self, effect_id, run_owner=run_owner, claim_owner=claim_owner, lease_seconds=lease_seconds)

    def renew_recovery_claim(
        self,
        effect_id: str,
        *,
        recovery_owner: str,
        lease_seconds: int,
    ) -> bool:
        return invocation_claims.renew_recovery_claim(self, effect_id, recovery_owner=recovery_owner, lease_seconds=lease_seconds)

    def _intent_run_id(self, effect_id: str) -> str:
        return invocation_claims.intent_run_id(self, effect_id)

    def abandon_invocation_claim(
        self,
        effect_id: str,
        *,
        run_owner: str,
        claim_owner: str,
    ) -> None:
        return invocation_claims.abandon_invocation_claim(self, effect_id, run_owner=run_owner, claim_owner=claim_owner)

    def _commit_result_locked(
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
        return result_transactions.commit_result_locked(self, db, intent, lease, owner=owner, result=result, failed=failed, reconciled=reconciled)

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
        return result_transactions.commit_result(self, effect_id, run_owner=run_owner, claim_owner=claim_owner, result=result, failed=failed, reconciled=reconciled, _conflict_retries=_conflict_retries)

    def mark_unknown(
        self,
        effect_id: str,
        *,
        owner: str,
        claim_owner: str,
        reason: str,
    ) -> None:
        return result_transactions.mark_unknown(self, effect_id, owner=owner, claim_owner=claim_owner, reason=reason)

    def terminal_decision(self, effect_id: str) -> Optional[types.IntentDecision]:
        return result_transactions.terminal_decision(self, effect_id)

    def pending_effect_ids(self) -> list[str]:
        return result_transactions.pending_effect_ids(self)

    def prune_settled(self, *, retention_days: int = 7) -> int:
        return retention.prune_settled(self, retention_days=retention_days)

    def settle_terminal_run_intent(self, effect_id: str) -> Optional[types.ToolIntent]:
        return retention.settle_terminal_run_intent(self, effect_id)

    def claim_recovery_intent(
        self,
        effect_id: str,
        *,
        recovery_owner: str,
        lease_seconds: int = 120,
    ) -> Optional[types.ToolIntent]:
        return recovery_claims.claim_recovery_intent(self, effect_id, recovery_owner=recovery_owner, lease_seconds=lease_seconds)

    def commit_recovered_result(
        self,
        effect_id: str,
        result: Any,
        *,
        recovery_owner: str,
    ) -> Any:
        return recovery_claims.commit_recovered_result(self, effect_id, result, recovery_owner=recovery_owner)

    def release_recovery_claim(
        self,
        effect_id: str,
        *,
        recovery_owner: str,
        retry_seconds: int = 30,
    ) -> None:
        return recovery_claims.release_recovery_claim(self, effect_id, recovery_owner=recovery_owner, retry_seconds=retry_seconds)
