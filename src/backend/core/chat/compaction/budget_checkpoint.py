"""Atomic context-budget stops under the run's current lease."""

from __future__ import annotations

from dataclasses import dataclass, field

from core.db.models import ChatMessage
from core.llm.context.safety import ContextBudgetExceeded
from core.runs.journal import RunJournal


class ContextBudgetJournal(RunJournal):
    def checkpoint_context(
        self, context: dict, stopped: ContextBudgetExceeded, checkpoint=None
    ) -> None:
        self._commit_stop(context, stopped, checkpoint, pause=False)

    def pause_context(self, context: dict, stopped: ContextBudgetExceeded, checkpoint=None) -> None:
        self._commit_stop(context, stopped, checkpoint, pause=True)

    def _commit_stop(self, context, stopped, checkpoint, *, pause):
        run_id, owner = context["run_id"], context["journal_owner"]
        with self._sessions() as db:
            run = self._locked_owned_run(db, run_id, owner, with_snapshot=True)
            message_id = (
                checkpoint.message_id
                if checkpoint and checkpoint.message_id
                else context.get("message_id") or run.message_id
            )
            message = (
                db.query(ChatMessage)
                .filter_by(
                    message_id=message_id,
                    chat_id=run.chat_id,
                    role="assistant",
                )
                .with_for_update()
                .one_or_none()
            )
            if message is not None and checkpoint is not None:
                message.content = checkpoint.content
                message.model_steps = list(checkpoint.steps) or None
            snapshot = {
                "manifest": stopped.manifest,
                "model_context": stopped.context,
                "summary": stopped.summary,
                "model_steps": (
                    list(checkpoint.steps)
                    if checkpoint
                    else (list(message.model_steps or []) if message is not None else [])
                ),
                "assistant_content": (
                    checkpoint.content
                    if checkpoint
                    else (message.content if message is not None else "")
                ),
                "message_id": message_id,
            }
            existing = (run.recovery_snapshot or {}).get("context_budget_stop")
            if stopped.saved and existing:
                snapshot = existing
            run.recovery_snapshot = {
                **(run.recovery_snapshot or {}),
                "context_budget_stop": snapshot,
            }
            run.snapshot_version = int(run.snapshot_version or 0) + 1
            self._append_locked(
                db,
                run,
                owner=owner,
                operation_type="context_budget_stopped",
                phase="context_budget_exceeded",
                safety="manual_review",
                payload={"reason": str(stopped)},
                snapshot_version=run.snapshot_version,
            )
            if pause:
                run.status = "needs_attention"
                run.failure_reason = str(stopped)
                run.lease_owner = None
                run.lease_expires_at = None
                run.writer_slot = None
            run.updated_at = self._clock()
            db.commit()
        stopped.saved = True


@dataclass
class ContextStopCheckpoint:
    steps: list[dict] = field(default_factory=list)
    content: str = ""
    message_id: str = ""

    def observe(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "model_step" and isinstance(event.get("step"), dict):
            self.steps.append(event["step"])
        elif kind == "tool_result" and isinstance(event.get("model_step"), dict):
            self.steps.append(event["model_step"])
        elif kind in {"content", "ai_message"}:
            self.content += event.get("delta") or ""
        elif kind == "content_replace":
            self.content = event.get("content") or ""
        elif kind == "steer_applied":
            self.message_id = event.get("next_assistant_message_id") or self.message_id
            self.steps = []
            self.content = ""

    def save(self, context: dict, stopped: ContextBudgetExceeded) -> None:
        if not context.get("run_id") or not context.get("journal_owner"):
            raise RuntimeError("context budget checkpoint requires an owned run")
        ContextBudgetJournal().checkpoint_context(context, stopped, self)
