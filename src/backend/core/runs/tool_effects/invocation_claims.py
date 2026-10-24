"""Acquire, renew and abandon invocation claims."""
from __future__ import annotations
from datetime import timedelta
from core.db.models import ChatRun, ToolEffectLease, ToolEffectLedger
from core.runs.journal import LIVE_STATUSES
from core.runs.tool_effects import errors

def acquire_invocation_claim(
    self,
    effect_id: str,
    *,
    run_owner: str,
    claim_owner: str,
    lease_seconds: int = 300,
) -> None:
    """Validate and extend one invocation claim in a short atomic transaction."""
    now = self._clock()
    expires = now + timedelta(seconds=max(1, lease_seconds))
    with self._sessions() as db:
        live_run = db.query(ChatRun.run_id).filter(
            ChatRun.status.in_(LIVE_STATUSES),
            ChatRun.lease_owner == run_owner,
            ChatRun.lease_expires_at > now,
        )
        affected = (
            db.query(ToolEffectLease)
            .filter(
                ToolEffectLease.effect_id == effect_id,
                ToolEffectLease.run_owner == run_owner,
                ToolEffectLease.claim_owner == claim_owner,
                ToolEffectLease.lease_expires_at > now,
                ToolEffectLease.run_id.in_(live_run),
            )
            .update(
                {
                    ToolEffectLease.lease_expires_at: expires,
                    ToolEffectLease.updated_at: now,
                },
                synchronize_session=False,
            )
        )
        if not affected or self._terminal_row(db, effect_id) is not None:
            db.rollback()
            raise errors.ToolEffectLeaseLost(effect_id)
        db.commit()


def renew_invocation_claim(
    self,
    effect_id: str,
    *,
    run_owner: str,
    claim_owner: str,
    lease_seconds: int,
) -> bool:
    if not self._runs.renew(
        self._intent_run_id(effect_id),
        owner=run_owner,
        lease_seconds=lease_seconds,
    ):
        return False
    try:
        self.acquire_invocation_claim(
            effect_id,
            run_owner=run_owner,
            claim_owner=claim_owner,
            lease_seconds=lease_seconds,
        )
        return True
    except errors.ToolEffectLeaseLost:
        return False


def renew_recovery_claim(
    self,
    effect_id: str,
    *,
    recovery_owner: str,
    lease_seconds: int,
) -> bool:
    return self.renew_invocation_claim(
        effect_id,
        run_owner=recovery_owner,
        claim_owner=recovery_owner,
        lease_seconds=lease_seconds,
    )


def intent_run_id(self, effect_id: str) -> str:
    with self._sessions() as db:
        row = (
            db.query(ToolEffectLedger.run_id)
            .filter(
                ToolEffectLedger.effect_id == effect_id,
                ToolEffectLedger.event_type == "intent",
            )
            .one()
        )
        return str(row[0])


def abandon_invocation_claim(
    self,
    effect_id: str,
    *,
    run_owner: str,
    claim_owner: str,
) -> None:
    """Expire a cancelled/ambiguous call so policy recovery can take over."""
    now = self._clock()
    with self._sessions() as db:
        db.query(ToolEffectLease).filter(
            ToolEffectLease.effect_id == effect_id,
            ToolEffectLease.run_owner == run_owner,
            ToolEffectLease.claim_owner == claim_owner,
        ).update(
            {
                ToolEffectLease.lease_expires_at: now,
                ToolEffectLease.updated_at: now,
            },
            synchronize_session=False,
        )
        db.commit()
