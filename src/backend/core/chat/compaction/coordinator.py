"""Watermarked compaction coordinator and its observable lifecycle."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional

from core.config import settings as config_settings
from core.llm.context import compaction as C
from core.llm.context import budget
from core.chat.compaction import summary as summary_model
from core.chat.compaction.history import normalize_rows
from core.chat.compaction.progress import finish_progress, report_outcome, start_progress

logger = logging.getLogger(__name__)


async def _settle_database_task(task: asyncio.Task):
    """Wait for a database thread even if cancellation is requested again."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise


class CompactionCoordinator:
    """Transaction coordinator shared by every compaction phase.

    The database session is deliberately short-lived on both sides of the LLM
    call: first acquire a source watermark + lease, then summarize without a DB
    connection, finally publish through a version/lease CAS.
    """

    def __init__(self, chat_id: str, *, run_id: str = "", hook_bus: Any = None):
        self.chat_id = chat_id
        self.run_id = run_id
        self.owner = f"compactor:{uuid.uuid4().hex}"
        # Outcome of the last ``compact`` call. ``None`` from ``compact`` means
        # both "nothing to do" and "failed"; the manual endpoint must tell them
        # apart. One of: compacted / nothing / busy / failed.
        self.outcome = ""
        self.tokens_before: Optional[int] = None
        self.tokens_after: Optional[int] = None
        if hook_bus is not None:
            self.hook_bus = hook_bus
        elif run_id:
            from core.harness.events import EventSink
            from core.harness.hooks import HookBus
            from core.harness.ledger import DurableEventStore, HarnessUsageLedger

            self.hook_bus = HookBus(
                event_sink=EventSink(DurableEventStore()),
                usage_recorder=HarnessUsageLedger(),
            )
        else:
            self.hook_bus = None

    def _acquire(self):
        from core.db.engine import SessionLocal
        from core.chat.service import ChatService

        with SessionLocal() as db:
            return ChatService(db).acquire_compaction_snapshot(
                self.chat_id,
                owner=self.owner,
                lease_seconds=max(
                    30, int(config_settings.settings.compaction.summarize_timeout_s) + 30
                ),
            )

    def _release(self) -> None:
        from core.db.engine import SessionLocal
        from core.chat.service import ChatService

        with SessionLocal() as db:
            ChatService(db).release_compaction_lease(self.chat_id, owner=self.owner)

    def _commit(
        self,
        snapshot: Any,
        *,
        summary_text: str,
        replacement: List[Dict[str, Any]],
        manifest: Dict[str, Any],
    ) -> None:
        from core.db.engine import SessionLocal
        from core.chat.service import ChatService

        with SessionLocal() as db:
            ChatService(db).commit_compaction_checkpoint(
                snapshot,
                owner=self.owner,
                summary_text=summary_text,
                replacement_history=replacement,
                replacement_manifest=manifest,
            )

    async def compact(
        self,
        *,
        phase: C.CompactionPhase = C.CompactionPhase.POST_TURN,
        in_memory_history: Optional[List[Dict[str, Any]]] = None,
        budget_inputs: Optional[Dict[str, Any]] = None,
        token_limit: Optional[int] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """Run one phase of the shared pipeline under a watermark + lease + CAS.

        ``phase`` does not change the algorithm; it selects which history is
        authoritative and is recorded on the checkpoint so a summary can be
        attributed to where the decision was taken.

        ``in_memory_history`` is only consulted for mid-turn compaction, where
        the agent may contain messages not yet visible in the DB snapshot.
        """

        self.outcome = "failed"
        self.tokens_before = self.tokens_after = None
        acquire_task = asyncio.create_task(asyncio.to_thread(self._acquire))
        try:
            snapshot = await asyncio.shield(acquire_task)
        except asyncio.CancelledError as cancellation:
            # The thread may acquire a lease after the coroutine is cancelled.
            # Wait for it, then release ownership before propagating cancellation.
            try:
                snapshot = await _settle_database_task(acquire_task)
                if snapshot is not None:
                    await _settle_database_task(
                        asyncio.create_task(asyncio.to_thread(self._release))
                    )
            except Exception:
                logger.warning(
                    "compaction_acquire_cleanup_failed chat=%s", self.chat_id, exc_info=True
                )
            raise cancellation
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[compaction] snapshot acquisition failed chat=%s: %r",
                self.chat_id,
                exc,
            )
            await report_outcome(self.chat_id, self.run_id, phase.value, "failed", "snapshot")
            return None
        if snapshot is None:
            # A live lease means another compactor owns the checkpoint. Mid-turn
            # compaction is overflow protection for the turn already running, so
            # it still summarizes and hands the result back for immediate use —
            # it just does not publish. Letting the context blow past the window
            # would be the worse failure.
            if phase is C.CompactionPhase.MID_TURN and in_memory_history:
                logger.info(
                    "[compaction] lease busy, mid-turn falls back to ephemeral chat=%s",
                    self.chat_id,
                )
                return await summary_model.summarize_without_checkpoint(
                    list(in_memory_history), chat_id=self.chat_id, run_id=self.run_id
                )
            self.outcome = "busy"
            await report_outcome(self.chat_id, self.run_id, phase.value, "skipped", "busy")
            return None

        published = False
        progress = None
        terminal_status = None
        try:
            persisted = list(snapshot.replacement_history) + normalize_rows(
                list(snapshot.source_rows)
            )
            # Summarize what the turn actually holds, but publish against the
            # snapshot's watermark so the lease/CAS guarantees are unchanged.
            history = (
                list(in_memory_history)
                if phase is C.CompactionPhase.MID_TURN and in_memory_history
                else persisted
            )
            if not history:
                self.outcome = "nothing"
                return None
            inputs = dict(budget_inputs or {})
            if self.hook_bus is not None:
                from core.harness.events import thaw_value
                from core.harness.hooks import HookStage, Invocation

                before = await self.hook_bus.enforce(
                    Invocation.create(
                        run_id=self.run_id,
                        stage=HookStage.BEFORE_COMPACTION,
                        operation_name="context_compaction",
                        data={"history": history, "budget": inputs},
                    )
                )
                if before.data.get("history") is not None:
                    history = thaw_value(before.data["history"])
                if before.data.get("budget") is not None:
                    inputs = thaw_value(before.data["budget"])
            inputs["messages"] = history
            if "system_prompt" not in inputs:
                from core.chat.compaction.summary import _load_base_system_prompt

                inputs["system_prompt"] = _load_base_system_prompt()
            estimate = budget.estimate_context_budget(**inputs)
            active_tokens = estimate["total_estimated_tokens"]
            measured_tokens = -1
            if phase is C.CompactionPhase.POST_TURN:
                measured_tokens = budget.resolve_snapshot_active_tokens(snapshot, inputs, -1)
                if measured_tokens > 0:
                    active_tokens = measured_tokens
            self.tokens_before = active_tokens
            if token_limit is not None and not budget.should_compact(active_tokens, token_limit):
                self.outcome = "nothing"
                return None

            cfg = config_settings.settings.compaction
            older, recent = C.split_history_for_compaction(
                history, keep_recent_tokens=cfg.keep_recent_tokens
            )
            if not older:
                # Everything fits in the verbatim tail; a summary would remove
                # nothing. The caller sees "nothing to compact", not a failure.
                logger.info(
                    "[compaction] nothing older than the kept tail chat=%s rows=%d",
                    self.chat_id,
                    len(recent),
                )
                self.outcome = "nothing"
                return None
            progress = await start_progress(self.chat_id, self.run_id, phase.value)
            from core.models.usage import model_usage_scope

            with model_usage_scope(
                self.run_id,
                self.hook_bus.usage_recorder if self.hook_bus is not None else None,
            ):
                summary = await summary_model._summarize(older, timeout=cfg.summarize_timeout_s)
            if not summary:
                return None

            summary_text = C.format_summary_text(summary)
            replacement = C.build_compacted_history(summary_text, recent)
            if self.hook_bus is not None:
                from core.harness.events import thaw_value
                from core.harness.hooks import HookStage, Invocation

                after = await self.hook_bus.enforce(
                    Invocation.create(
                        run_id=self.run_id,
                        stage=HookStage.AFTER_COMPACTION,
                        operation_name="context_compaction",
                        data={"replacement": replacement, "metadata": {}},
                    )
                )
                if after.data.get("replacement") is not None:
                    replacement = thaw_value(after.data["replacement"])
            manifest: Dict[str, Any] = {
                "phase": phase.value,
                "source_high_watermark_seq": snapshot.covered_seq,
                "source_hash": snapshot.source_hash,
                "base_checkpoint_version": snapshot.base_checkpoint_version,
                "budget_estimate": estimate,
                "trigger_active_tokens": active_tokens,
                "trigger_token_limit": token_limit,
                "trigger_source": ("provider" if measured_tokens > 0 else "backend_estimate"),
            }
            replacement_inputs = {**inputs, "messages": replacement}
            replacement_estimate = budget.estimate_context_budget(**replacement_inputs)
            from core.llm.context.usage import build_compaction_context_usage

            manifest["replacement_budget_estimate"] = replacement_estimate
            self.tokens_after = replacement_estimate["total_estimated_tokens"]
            manifest["replacement_context_usage"] = build_compaction_context_usage(
                replacement_estimate,
                context_window=int(inputs.get("context_window") or 0),
                model_name=str(inputs.get("model_name") or ""),
                model_provider_id=str(inputs.get("model_provider_id") or ""),
            )
            # A database thread cannot be cancelled. Resolve its result before
            # reporting the terminal state or releasing its lease.
            commit_task = asyncio.create_task(
                asyncio.to_thread(
                    self._commit,
                    snapshot,
                    summary_text=summary_text,
                    replacement=replacement,
                    manifest=manifest,
                )
            )
            try:
                await asyncio.shield(commit_task)
            except asyncio.CancelledError as cancellation:
                try:
                    await _settle_database_task(commit_task)
                    published = True
                except Exception:
                    terminal_status = "failed"
                    logger.warning(
                        "compaction_commit_failed_after_cancel chat=%s", self.chat_id, exc_info=True
                    )
                raise cancellation
            published = True
            self.outcome = "compacted"
        except Exception as exc:  # noqa: BLE001
            from core.harness.hooks import HookPaused

            if isinstance(exc, HookPaused):
                raise
            # Persistence failure is an explicit failure: no replacement is
            # returned to the turn and no success/notice is fabricated.
            logger.warning("[compaction] checkpoint commit failed chat=%s: %r", self.chat_id, exc)
            if progress is None:
                await report_outcome(
                    self.chat_id, self.run_id, phase.value, "failed", "preparation"
                )
            return None
        finally:
            try:
                if progress is not None:
                    task = asyncio.current_task()
                    await finish_progress(
                        progress,
                        terminal_status
                        or (
                            "completed"
                            if published
                            else "interrupted" if task and task.cancelling() else "failed"
                        ),
                    )
            finally:
                if not published:
                    try:
                        await _settle_database_task(
                            asyncio.create_task(asyncio.to_thread(self._release))
                        )
                    except Exception as release_exc:  # noqa: BLE001
                        logger.debug(
                            "[compaction] lease release failed chat=%s: %r",
                            self.chat_id,
                            release_exc,
                        )

        logger.info(
            "[compaction] checkpoint written chat=%s phase=%s covered_seq=%d "
            "msgs=%d→%d summary_len=%d",
            self.chat_id,
            phase.value,
            snapshot.covered_seq,
            len(history),
            len(replacement),
            len(summary_text),
        )
        return replacement
