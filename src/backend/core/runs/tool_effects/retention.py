"""Prune settled tool effect records and close orphaned intents."""
from __future__ import annotations
from datetime import timedelta
from typing import Optional
from core.db.models import ChatRun, ToolEffectLease, ToolEffectLedger
from core.runs.journal import LIVE_STATUSES
from sqlalchemy import and_, case
from core.runs.tool_effects import journal_constants
from core.runs.tool_effects import types
from core.infra.time import as_utc

def prune_settled(self, *, retention_days: int = 7) -> int:
    """Drop per-run recovery/observability facts of long-terminal runs.

    Covers the tool effect ledger and leases plus the other append-only
    per-run journals (harness usage attempts, harness events, run
    operations); without this they grow without bound.

    The run's own recovery columns go the same way. ``recovery_snapshot``
    carries the whole conversation as of that turn, so a long chat stores
    roughly its own history once per turn — the single largest thing in a
    desktop database. Only crash recovery reads it, and only for runs that
    are still live and still replayable, which is exactly what the cutoff
    and the settled filter below exclude.

    ``needs_attention`` is kept ``_ATTENTION_RETENTION_FACTOR`` times longer
    rather than exempted, and that applies to every tier above. Exempting it
    means never: nothing in the backend transitions a run out of that status,
    so an exemption pins its journals — and its snapshot — for good. In
    production every record that had outlived the retention window belonged
    to such a run.
    """
    from core.db.models import ChatRunOperation, HarnessEventLog, HarnessUsageAttempt

    now = as_utc(self._clock())
    cutoff = now - timedelta(days=retention_days)
    attention_cutoff = now - timedelta(days=retention_days * journal_constants.ATTENTION_RETENTION_FACTOR)
    with self._sessions() as db:

        earliest_cutoff = max(cutoff, attention_cutoff)

        def _prunable(run_col, stamp):
            """Row whose run is terminal and past that run's retention tier.

            ``stamp < earliest_cutoff`` is implied by the EXISTS (it is the
            loosest of the tiers) and stated anyway: it is the only
            predicate on these tables an index can serve, and without it the
            planner has nothing to range-scan on.
            """
            tier = case(
                (ChatRun.status == "needs_attention", attention_cutoff),
                else_=cutoff,
            )
            return and_(
                stamp < earliest_cutoff,
                db.query(ChatRun.run_id)
                .filter(
                    ChatRun.run_id == run_col,
                    ChatRun.status.notin_(LIVE_STATUSES),
                    stamp < tier,
                )
                .exists(),
            )

        deleted = (
            db.query(ToolEffectLedger)
            .filter(_prunable(ToolEffectLedger.run_id, ToolEffectLedger.created_at))
            .delete(synchronize_session=False)
        )
        db.query(ToolEffectLease).filter(
            _prunable(ToolEffectLease.run_id, ToolEffectLease.updated_at)
        ).delete(synchronize_session=False)
        for model in (HarnessUsageAttempt, HarnessEventLog, ChatRunOperation):
            deleted += (
                db.query(model)
                .filter(_prunable(model.run_id, model.created_at))
                .delete(synchronize_session=False)
            )
        deleted += (
            db.query(ChatRun)
            .filter(
                ChatRun.status.notin_(LIVE_STATUSES),
                ChatRun.completed_at < earliest_cutoff,
                ChatRun.completed_at
                < case(
                    (ChatRun.status == "needs_attention", attention_cutoff),
                    else_=cutoff,
                ),
                ChatRun.recovery_snapshot.isnot(None),
            )
            .update(
                {ChatRun.recovery_snapshot: None, ChatRun.request_payload: None},
                synchronize_session=False,
            )
        )
        db.commit()
        return deleted


def settle_terminal_run_intent(self, effect_id: str) -> Optional[types.ToolIntent]:
    """Close an orphan intent without replay after its owning run is terminal.

    Cancellation/failure is a hard replay boundary: the user's stopped run
    must never perform a delayed external action.  We still append an
    ``unknown_outcome`` fact so the intent cannot remain pending forever.
    """
    with self._sessions() as db:
        intent = (
            db.query(ToolEffectLedger)
            .filter(
                ToolEffectLedger.effect_id == effect_id,
                ToolEffectLedger.event_type == "intent",
            )
            .with_for_update()
            .one_or_none()
        )
        if intent is None or self._terminal_row(db, effect_id) is not None:
            return None
        run = (
            db.query(ChatRun)
            .filter(ChatRun.run_id == intent.run_id)
            .with_for_update()
            .one_or_none()
        )
        if run is None or run.status in (*LIVE_STATUSES, "needs_attention"):
            return None
        lease = (
            db.query(ToolEffectLease)
            .filter(ToolEffectLease.effect_id == effect_id)
            .with_for_update()
            .one()
        )
        terminal_status = str(run.status)
        self._append_event(
            db,
            run,
            intent,
            owner="system:terminal-settlement",
            event_type="unknown_outcome",
            operation_type="tool_outcome_unknown_after_terminal_run",
            phase=str(run.run_phase or terminal_status),
            safety="manual_review",
            error=f"run became {terminal_status} before the tool outcome committed",
            terminal=True,
        )
        # _append_event advances only the operation ledger. Preserve the
        # authoritative terminal verdict and merely close the effect claim.
        run.status = terminal_status
        lease.claim_owner = None
        lease.lease_expires_at = None
        lease.updated_at = self._clock()
        run.updated_at = self._clock()
        db.commit()
        return self._intent_from_row(intent)
