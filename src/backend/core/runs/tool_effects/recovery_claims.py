"""Claim and settle intents during recovery."""
from __future__ import annotations
from datetime import timedelta
from typing import Any, Optional
from core.db.models import ChatRun, ToolEffectLease, ToolEffectLedger
from core.runs.journal import LIVE_STATUSES
from sqlalchemy import or_
from core.runs.tool_effects import errors
from core.runs.tool_effects import types

def claim_recovery_intent(
    self,
    effect_id: str,
    *,
    recovery_owner: str,
    lease_seconds: int = 120,
) -> Optional[types.ToolIntent]:
    with self._sessions() as db:
        intent = (
            db.query(ToolEffectLedger)
            .filter(
                ToolEffectLedger.effect_id == effect_id,
                ToolEffectLedger.event_type == "intent",
            )
            .one_or_none()
        )
        if intent is None or self._terminal_row(db, effect_id) is not None:
            return None
        now = self._clock()
        from core.auth.api_key_runs import authorize_run_execution

        if not authorize_run_execution(db, intent.run_id, now):
            return None
        expires = now + timedelta(seconds=max(1, lease_seconds))
        run_affected = (
            db.query(ChatRun)
            .filter(
                ChatRun.run_id == intent.run_id,
                ChatRun.status.in_((*LIVE_STATUSES, "needs_attention")),
                or_(
                    ChatRun.lease_owner.is_(None),
                    ChatRun.lease_expires_at.is_(None),
                    ChatRun.lease_expires_at <= now,
                    ChatRun.lease_owner == recovery_owner,
                ),
            )
            .update(
                {
                    ChatRun.status: "running",
                    ChatRun.lease_owner: recovery_owner,
                    ChatRun.lease_expires_at: expires,
                    ChatRun.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if not run_affected:
            db.rollback()
            return None
        lease_affected = (
            db.query(ToolEffectLease)
            .filter(
                ToolEffectLease.effect_id == effect_id,
                or_(
                    ToolEffectLease.lease_expires_at.is_(None),
                    ToolEffectLease.lease_expires_at <= now,
                    ToolEffectLease.claim_owner == recovery_owner,
                ),
            )
            .update(
                {
                    ToolEffectLease.run_owner: recovery_owner,
                    ToolEffectLease.claim_owner: recovery_owner,
                    ToolEffectLease.lease_expires_at: expires,
                    ToolEffectLease.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if not lease_affected or self._terminal_row(db, effect_id) is not None:
            db.rollback()
            return None
        db.expire_all()
        run = db.get(ChatRun, intent.run_id)
        self._append_event(
            db,
            run,
            intent,
            owner=recovery_owner,
            event_type="recovery_claim",
            operation_type="tool_effect_recovery_claimed",
            phase=f"tool_intent_{intent.recovery_policy}",
            safety=intent.recovery_policy,
        )
        db.commit()
        return self._intent_from_row(intent)


def commit_recovered_result(
    self,
    effect_id: str,
    result: Any,
    *,
    recovery_owner: str,
) -> Any:
    value = self.commit_result(
        effect_id,
        run_owner=recovery_owner,
        claim_owner=recovery_owner,
        result=result,
        reconciled=True,
    )
    with self._sessions() as db:
        lease = (
            db.query(ToolEffectLease)
            .filter(ToolEffectLease.effect_id == effect_id)
            .with_for_update()
            .one()
        )
        run = (
            db.query(ChatRun)
            .filter(ChatRun.run_id == lease.run_id)
            .with_for_update()
            .one()
        )
        if lease.run_owner != recovery_owner or run.lease_owner != recovery_owner:
            raise errors.ToolEffectLeaseLost(effect_id)
        run.lease_owner = None
        run.lease_expires_at = None
        run.updated_at = self._clock()
        db.commit()
    return value


def release_recovery_claim(
    self,
    effect_id: str,
    *,
    recovery_owner: str,
    retry_seconds: int = 30,
) -> None:
    """Defer a failed adapter without letting RunJournal resume past the pending effect."""
    with self._sessions() as db:
        lease = (
            db.query(ToolEffectLease)
            .filter(ToolEffectLease.effect_id == effect_id)
            .with_for_update()
            .one()
        )
        if lease.run_owner != recovery_owner or lease.claim_owner != recovery_owner:
            raise errors.ToolEffectLeaseLost(effect_id)
        run = (
            db.query(ChatRun)
            .filter(ChatRun.run_id == lease.run_id)
            .with_for_update()
            .one()
        )
        if run.lease_owner != recovery_owner:
            raise errors.ToolEffectLeaseLost(effect_id)
        retry_at = self._clock() + timedelta(seconds=max(1, retry_seconds))
        lease.claim_owner = None
        lease.lease_expires_at = retry_at
        lease.updated_at = self._clock()
        run.lease_expires_at = retry_at
        run.updated_at = self._clock()
        db.commit()
