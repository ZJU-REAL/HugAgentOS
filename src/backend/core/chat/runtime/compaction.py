"""Schedule background compression using the completed turn's frozen window."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from core.infra.logging import get_logger

logger = get_logger(__name__)


def _should_schedule_compaction(
    *,
    model_name: str,
    usage: Optional[Dict] = None,
    budget_inputs: Optional[Dict[str, Any]] = None,
) -> bool:
    """Return whether the post-turn compactor will be scheduled."""
    try:
        from core.config.settings import settings as _settings

        if not _settings.compaction.enabled:
            return False
        from core.models.context_window import resolve_model_context_window
        from core.chat.compaction.service import resolve_active_tokens, resolve_token_limit, resolve_trigger_ratio, should_compact

        active_tokens = resolve_active_tokens(usage)
        window = int((budget_inputs or {}).get("context_window") or 0)
        limit = resolve_token_limit(
            window if window > 0 else resolve_model_context_window(model_name or ""),
            ratio=resolve_trigger_ratio(),
        )
        return should_compact(active_tokens, limit)
    except Exception:
        logger.debug("compaction_schedule_check_failed", exc_info=True)
        return False


def _spawn_compaction_task(
    *,
    run_id: str,
    chat_id: str,
    model_name: str,
    usage: Optional[Dict] = None,
    budget_inputs: Optional[Dict[str, Any]] = None,
    should_schedule: Optional[bool] = None,
    message_id: str = "",
) -> None:
    """PostTurn phase: background pre-warm of the shared compaction engine.

    Fire-and-forget; never affects the main conversation. This is *not* a second
    compactor — it calls the same
    :func:`core.chat.compaction.service.run_compaction` the mid-turn hook
    does, and normally finds nothing to do: a turn that crossed the threshold
    while it still had steps left has already compacted mid-turn and persisted
    its checkpoint. It earns its keep only when the threshold is crossed by a
    turn's *final* reasoning step, where there is no later step boundary to
    catch it; doing it here rather than at the next turn's PreTurn check keeps
    the summarisation off the user-visible latency path.

    ``run_id`` and ``budget_inputs`` are threaded through to the Coordinator so
    the checkpoint is published under the run's hook bus and usage ledger, and
    so its component estimator reuses the turn's already-frozen execution
    surface instead of re-deriving one.
    """
    schedule = (
        _should_schedule_compaction(model_name=model_name, usage=usage, budget_inputs=budget_inputs)
        if should_schedule is None
        else should_schedule
    )
    if not schedule:
        return

    async def _bg() -> None:
        try:
            from core.models.context_window import resolve_model_context_window
            from core.chat.compaction.service import resolve_token_limit, resolve_trigger_ratio, run_post_turn_compaction

            # Trigger criterion — see resolve_active_tokens: real end-of-turn
            # context occupancy, not the cumulative billing value. Gating here
            # keeps a turn that never approached the window from taking the
            # compaction lease at all; the Coordinator re-checks against the
            # authoritative snapshot measurement before calling the summariser.
            window = int((budget_inputs or {}).get("context_window") or 0)
            limit = resolve_token_limit(
                window if window > 0 else resolve_model_context_window(model_name or ""),
                ratio=resolve_trigger_ratio(),
            )
            logger.info(
                "chat_compaction_evaluating",
                chat_id=chat_id,
                limit=limit,
            )
            from core.chat.compaction.progress import progress_scope

            with progress_scope(None, chat_id, run_id, context={"message_id": message_id}):
                await run_post_turn_compaction(
                    chat_id,
                    budget_inputs=dict(budget_inputs or {}),
                    token_limit=limit,
                    run_id=run_id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("background_compaction_failed", error=str(exc))

    try:
        asyncio.create_task(_bg())
    except RuntimeError:
        pass
