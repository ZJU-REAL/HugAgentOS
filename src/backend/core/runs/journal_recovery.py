"""Classify orphaned runs without replaying uncertain side effects."""

from __future__ import annotations

from core.db.models import ChatRun
from core.runs.journal_domain import LIVE_STATUSES, NON_CHAT_RUN_KINDS, RECOVERABLE_PRE_MODEL_PHASES, RECOVERABLE_TOOL_PHASES, SAFE_STOP_ORPHAN_KINDS, RecoveryDecision
from sqlalchemy import or_
from sqlalchemy.orm import undefer


class RunRecovery:
    def recover(self) -> list[RecoveryDecision]:
        """Classify claimable orphans from the last committed DB safe point."""
        now = self._clock()
        decisions: list[RecoveryDecision] = []
        with self._sessions() as db:
            rows = (
                db.query(ChatRun)
                .options(undefer(ChatRun.recovery_snapshot))
                .filter(
                    ChatRun.status.in_(LIVE_STATUSES),
                    or_(
                        ChatRun.lease_owner.is_(None),
                        ChatRun.lease_expires_at.is_(None),
                        ChatRun.lease_expires_at <= now,
                    ),
                )
                .order_by(ChatRun.created_at, ChatRun.run_id)
                .with_for_update()
                .all()
            )
            for row in rows:
                snapshot = dict(row.recovery_snapshot or {})
                request_payload = dict(row.request_payload or {})
                phase = str(row.run_phase or "accepted")
                legacy_unrecoverable = (
                    phase == "legacy_unrecoverable"
                    and request_payload.get("kind") not in NON_CHAT_RUN_KINDS
                )
                snapshot_kind = snapshot.get("kind")
                if snapshot_kind in SAFE_STOP_ORPHAN_KINDS:
                    action = "needs_attention"
                    reason = f"{snapshot_kind} worker disappeared; owner-specific retry is required"
                    self._append_locked(
                        db,
                        row,
                        owner="system:recovery",
                        operation_type="run_needs_attention",
                        phase="needs_attention",
                        safety="manual_review",
                        payload={"reason": reason},
                    )
                    row.status = "needs_attention"
                    row.failure_reason = reason
                    row.lease_owner = None
                    row.lease_expires_at = None
                    decisions.append(
                        RecoveryDecision(
                            run_id=row.run_id,
                            chat_id=row.chat_id,
                            user_id=row.user_id,
                            message_id=row.message_id,
                            action=action,
                            phase=phase,
                            snapshot=snapshot,
                            request_payload=request_payload,
                        )
                    )
                    continue
                if snapshot_kind != "chat" and row.writer_slot == "main" and phase == "accepted":
                    reason = "server restarted before the accepted run was prepared"
                    self._append_locked(
                        db,
                        row,
                        owner="system:recovery",
                        operation_type="run_failed",
                        phase="failed",
                        safety="terminal",
                        payload={"reason": reason},
                    )
                    row.status = "failed"
                    row.failure_reason = reason
                    row.error_message = reason
                    row.completed_at = now
                    row.lease_owner = None
                    row.lease_expires_at = None
                    row.writer_slot = None
                    row.updated_at = now
                    decisions.append(
                        RecoveryDecision(
                            run_id=row.run_id,
                            chat_id=row.chat_id,
                            user_id=row.user_id,
                            message_id=row.message_id,
                            action="failed",
                            phase=phase,
                            snapshot=snapshot,
                            request_payload=request_payload,
                        )
                    )
                    continue
                if snapshot_kind != "chat" and not legacy_unrecoverable:
                    continue
                action = "resume"
                if legacy_unrecoverable:
                    action = "needs_attention"
                    reason = "legacy live run has no durable recovery snapshot"
                    self._append_locked(
                        db,
                        row,
                        owner="system:recovery",
                        operation_type="run_needs_attention",
                        phase="needs_attention",
                        safety="manual_review",
                        payload={"reason": reason},
                    )
                    row.status = "needs_attention"
                    row.failure_reason = reason
                elif phase == "message_committed":
                    action = "completed"
                    self._append_locked(
                        db,
                        row,
                        owner="system:recovery",
                        operation_type="recovered_after_message_commit",
                        phase="completed",
                        safety="terminal",
                        payload=None,
                    )
                    row.status = "completed"
                    row.completed_at = now
                elif (
                    phase == "tool_result_unknown"
                    or row.last_operation_safety == "unknown_side_effect"
                ):
                    action = "needs_attention"
                    self._append_locked(
                        db,
                        row,
                        owner="system:recovery",
                        operation_type="run_needs_attention",
                        phase="needs_attention",
                        safety="manual_review",
                        payload={"reason": "unknown tool result requires recovery decision"},
                    )
                    row.status = "needs_attention"
                    row.failure_reason = "unknown tool result requires recovery decision"
                elif phase in RECOVERABLE_TOOL_PHASES:
                    # We can recover the exact adapter call, but AgentScope's
                    # provider-neutral continuation cursor is not durable yet.
                    # Re-running the original prompt could produce a new tool
                    # call id and duplicate the side effect, so stop safely.
                    action = "needs_attention"
                    reason = (
                        "tool effect was recovered, but exact model continuation "
                        "is unavailable; automatic prompt replay is unsafe"
                    )
                    self._append_locked(
                        db,
                        row,
                        owner="system:recovery",
                        operation_type="run_needs_attention",
                        phase="needs_attention",
                        safety="manual_review",
                        payload={"reason": reason},
                    )
                    row.status = "needs_attention"
                    row.failure_reason = reason
                elif phase == "model_completed" and snapshot:
                    action = "resume_from_snapshot"
                elif phase == "model_inflight":
                    action = "needs_attention"
                    reason = "model outcome is unknown; automatic replay is unsafe"
                    self._append_locked(
                        db,
                        row,
                        owner="system:recovery",
                        operation_type="run_needs_attention",
                        phase="needs_attention",
                        safety="manual_review",
                        payload={"reason": reason},
                    )
                    row.status = "needs_attention"
                    row.failure_reason = reason
                elif phase not in RECOVERABLE_PRE_MODEL_PHASES:
                    action = "needs_attention"
                    self._append_locked(
                        db,
                        row,
                        owner="system:recovery",
                        operation_type="run_needs_attention",
                        phase="needs_attention",
                        safety="manual_review",
                        payload={"reason": f"unsupported recovery phase: {phase}"},
                    )
                    row.status = "needs_attention"
                    row.failure_reason = f"unsupported recovery phase: {phase}"

                if action in {"completed", "needs_attention"}:
                    row.lease_owner = None
                    row.lease_expires_at = None
                    row.writer_slot = None
                    row.updated_at = now
                decisions.append(
                    RecoveryDecision(
                        run_id=row.run_id,
                        chat_id=row.chat_id,
                        user_id=row.user_id,
                        message_id=row.message_id,
                        action=action,
                        phase=phase,
                        snapshot=snapshot,
                        request_payload=request_payload,
                    )
                )
            db.commit()
        return decisions
