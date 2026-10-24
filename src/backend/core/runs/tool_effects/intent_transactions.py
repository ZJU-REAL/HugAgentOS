"""Record tool intents and decide whether a call may run."""
from __future__ import annotations
import uuid
from datetime import timedelta
from typing import Any, Mapping, Optional
from core.db.models import ChatRun, ToolEffectLease, ToolEffectLedger
from core.runs.journal import LIVE_STATUSES, RunLeaseLost, RunNotFound
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from core.runs.tool_effects import arguments
from core.runs.tool_effects import errors
from core.runs.tool_effects import registry
from core.runs.tool_effects import types
from core.infra.time import as_utc

def intent_from_row(row: ToolEffectLedger) -> types.ToolIntent:
    return types.ToolIntent(
        effect_id=row.effect_id,
        result_id=row.result_id,
        run_id=row.run_id,
        operation_seq=int(row.operation_seq),
        tool_name=row.tool_name,
        tool_call_id=row.tool_call_id or "",
        args_hash=row.args_hash,
        redacted_args=dict(row.redacted_args or {}),
        idempotency_key=row.idempotency_key or "",
        recovery_policy=row.recovery_policy,
        created_at=as_utc(row.created_at),
    )


def terminal_row(db: Session, effect_id: str) -> Optional[ToolEffectLedger]:
    return (
        db.query(ToolEffectLedger)
        .filter(ToolEffectLedger.terminal_effect_id == effect_id)
        .one_or_none()
    )


def decision_from_terminal(
    intent: types.ToolIntent, terminal: ToolEffectLedger
) -> types.IntentDecision:
    if terminal.event_type == "unknown_outcome":
        return types.IntentDecision("unknown", intent, result_state="unknown")
    return types.IntentDecision(
        "known",
        intent,
        terminal.result_payload,
        "success" if terminal.event_type == "result" else "error",
    )


def validate_identity(
    row: ToolEffectLedger, *, tool_name: str, args_hash: str
) -> None:
    if row.tool_name != tool_name or row.args_hash != args_hash:
        raise errors.ToolIdempotencyConflict(
            "idempotency_key already belongs to different tool arguments"
        )


def existing_decision(
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
    with self._sessions() as db:
        intent_row = (
            db.query(ToolEffectLedger)
            .filter(ToolEffectLedger.idempotency_key == key)
            .one()
        )
        self._validate_identity(
            intent_row, tool_name=tool_name, args_hash=args_hash
        )
        intent = self._intent_from_row(intent_row)
        caller_run = db.get(ChatRun, caller_run_id)
        original_run = db.get(ChatRun, intent.run_id)
        if caller_run is None or original_run is None:
            raise errors.ToolIntentCommitError(f"run does not exist: {caller_run_id}")
        if original_run.user_id != caller_run.user_id:
            raise errors.ToolIdempotencyConflict(
                "idempotency_key belongs to a different principal"
            )
        terminal = self._terminal_row(db, intent.effect_id)
        if terminal is not None and caller_run_id == intent.run_id:
            return self._decision_from_terminal(intent, terminal)
        now = as_utc(self._clock())
        caller_expiry = as_utc(caller_run.lease_expires_at)
        if (
            caller_run.status not in LIVE_STATUSES
            or caller_run.lease_owner != run_owner
            or caller_expiry is None
            or now is None
            or caller_expiry <= now
        ):
            raise errors.ToolIntentCommitError(f"run lease is not owned: {caller_run_id}")
        if terminal is not None:
            return self._decision_from_terminal(intent, terminal)
        if intent.run_id != caller_run_id:
            return types.IntentDecision("wait", intent)

        lease = (
            db.query(ToolEffectLease)
            .filter(ToolEffectLease.effect_id == intent.effect_id)
            .with_for_update()
            .one()
        )
        run = self._runs._locked_owned_run(db, caller_run_id, run_owner)
        terminal = self._terminal_row(db, intent.effect_id)
        if terminal is not None:
            return self._decision_from_terminal(intent, terminal)
        lease_expiry = as_utc(lease.lease_expires_at)
        if (
            lease.run_owner == run_owner
            and lease_expiry is not None
            and lease_expiry > now
        ):
            if lease.claim_owner == claim_owner:
                return types.IntentDecision(
                    "reconcile"
                    if intent.recovery_policy == "reconcile"
                    else "execute",
                    intent,
                )
            return types.IntentDecision("wait", intent)

        previous_owner = lease.run_owner
        previous_claim = lease.claim_owner
        previous_expiry = lease.lease_expires_at
        affected = (
            db.query(ToolEffectLease)
            .filter(
                ToolEffectLease.effect_id == intent.effect_id,
                ToolEffectLease.run_owner == previous_owner,
                ToolEffectLease.claim_owner == previous_claim,
                ToolEffectLease.lease_expires_at == previous_expiry,
                or_(
                    ToolEffectLease.lease_expires_at.is_(None),
                    ToolEffectLease.lease_expires_at <= self._clock(),
                ),
            )
            .update(
                {
                    ToolEffectLease.run_owner: run_owner,
                    ToolEffectLease.claim_owner: claim_owner,
                    ToolEffectLease.lease_expires_at: self._clock()
                    + timedelta(seconds=max(1, lease_seconds)),
                    ToolEffectLease.updated_at: self._clock(),
                },
                synchronize_session=False,
            )
        )
        if not affected:
            db.rollback()
            return types.IntentDecision("wait", intent)
        db.expire_all()
        lease = db.get(ToolEffectLease, intent.effect_id)
        self._append_event(
            db,
            run,
            intent_row,
            owner=run_owner,
            event_type="recovery_claim",
            operation_type="tool_effect_recovery_claimed",
            phase=f"tool_intent_{intent.recovery_policy}",
            safety=intent.recovery_policy,
        )
        if intent.recovery_policy == "never_replay":
            self._mark_unknown_locked(
                db,
                run,
                intent_row,
                lease,
                owner=run_owner,
                claim_owner=claim_owner,
                reason="non-replayable tool intent has no committed result",
            )
            db.commit()
            return types.IntentDecision("unknown", intent)
        db.commit()
        return types.IntentDecision(
            "reconcile" if intent.recovery_policy == "reconcile" else "execute",
            intent,
        )


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
    if recovery_policy not in registry.RECOVERY_POLICIES:
        raise ValueError(f"invalid recovery policy: {recovery_policy}")
    _canonical, args_hash, redacted = arguments.canonical_tool_args(args)
    with self._sessions() as db:
        caller_run = db.get(ChatRun, run_id)
        if caller_run is None:
            raise errors.ToolIntentCommitError(f"run does not exist: {run_id}")
        principal_scope = caller_run.user_id
    key, _explicit = arguments.resolved_idempotency_key(
        run_id=run_id,
        principal_scope=str(principal_scope),
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        explicit_key=idempotency_key
        or str(args.get("idempotency_key") or args.get("request_id") or "").strip(),
    )
    try:
        with self._sessions() as db:
            found = (
                db.query(ToolEffectLedger.event_id)
                .filter(ToolEffectLedger.idempotency_key == key)
                .first()
            )
        if found is not None:
            return self._existing_decision(
                key=key,
                caller_run_id=run_id,
                run_owner=owner,
                claim_owner=claim_owner,
                tool_name=tool_name,
                args_hash=args_hash,
                lease_seconds=lease_seconds,
            )

        with self._sessions() as db:
            run = self._runs._locked_owned_run(db, run_id, owner)
            found = (
                db.query(ToolEffectLedger.event_id)
                .filter(ToolEffectLedger.idempotency_key == key)
                .first()
            )
            if found is not None:
                db.rollback()
                return self._existing_decision(
                    key=key,
                    caller_run_id=run_id,
                    run_owner=owner,
                    claim_owner=claim_owner,
                    tool_name=tool_name,
                    args_hash=args_hash,
                    lease_seconds=lease_seconds,
                )
            effect_id = f"eff_{uuid.uuid4().hex}"
            result_id = f"toolres_{uuid.uuid4().hex}"
            receipt = self._runs._append_locked(
                db,
                run,
                owner=owner,
                operation_type="tool_intent_committed",
                phase=f"tool_intent_{recovery_policy}",
                safety=recovery_policy,
                payload={
                    "effect_id": effect_id,
                    "result_id": result_id,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "args_hash": args_hash,
                },
            )
            row = ToolEffectLedger(
                effect_id=effect_id,
                result_id=result_id,
                run_id=run_id,
                operation_seq=receipt.operation_seq,
                event_type="intent",
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                args_hash=args_hash,
                redacted_args=dict(redacted),
                idempotency_key=key,
                recovery_policy=recovery_policy,
            )
            db.add(row)
            db.add(
                ToolEffectLease(
                    effect_id=effect_id,
                    run_id=run_id,
                    run_owner=owner,
                    claim_owner=claim_owner,
                    lease_expires_at=self._clock()
                    + timedelta(seconds=max(1, lease_seconds)),
                    updated_at=self._clock(),
                )
            )
            run.updated_at = self._clock()
            db.commit()
            return types.IntentDecision("execute", self._intent_from_row(row))
    except errors.ToolEffectError:
        raise
    except (IntegrityError, OperationalError) as exc:
        with self._sessions() as db:
            found = (
                db.query(ToolEffectLedger.event_id)
                .filter(ToolEffectLedger.idempotency_key == key)
                .first()
            )
        if found is not None:
            return self._existing_decision(
                key=key,
                caller_run_id=run_id,
                run_owner=owner,
                claim_owner=claim_owner,
                tool_name=tool_name,
                args_hash=args_hash,
                lease_seconds=lease_seconds,
            )
        if _conflict_retries > 0:
            return self.begin_intent(
                run_id=run_id,
                owner=owner,
                claim_owner=claim_owner,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                args=args,
                recovery_policy=recovery_policy,
                idempotency_key=idempotency_key,
                lease_seconds=lease_seconds,
                _conflict_retries=_conflict_retries - 1,
            )
        raise errors.ToolIntentCommitError(str(exc)) from exc
    except (RunNotFound, RunLeaseLost) as exc:
        raise errors.ToolIntentCommitError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise errors.ToolIntentCommitError(str(exc)) from exc
