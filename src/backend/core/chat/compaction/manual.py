"""On-demand compaction between turns (``POST /v1/chats/{chat_id}/compact``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from core.config import settings as config_settings
from core.db.threads import run_in_session
from core.llm.context import compaction as C
from core.chat.compaction.coordinator import CompactionCoordinator
from core.chat.compaction.usage_projection import get_chat_context_state

_ATTRIBUTION_KEYS = ("context_window", "model_name", "model_provider_id")


@dataclass(frozen=True)
class ManualCompaction:
    """``outcome``: the coordinator's (compacted / nothing / busy / failed) or disabled."""

    outcome: str
    tokens_before: Optional[int] = None
    tokens_after: Optional[int] = None


def manual_budget_inputs(chat_service: Any, chat_id: str) -> Dict[str, Any]:
    """Attribute the checkpoint to the model the chat last measured.

    With the same window / model / provider as the last provider measurement,
    the coordinator reuses that exact token count as ``tokens_before`` instead
    of an estimate, and the context meter keeps its window after compaction.
    """
    usage = get_chat_context_state(chat_service, chat_id).get("context_usage") or {}
    return {key: usage[key] for key in _ATTRIBUTION_KEYS if usage.get(key)}


def _lease_is_live(db: Any, chat_id: str) -> bool:
    from core.chat.compaction_snapshots import compaction_lease_held
    from core.db.models import ChatCompactionState

    return compaction_lease_held(db.get(ChatCompactionState, chat_id))


async def compact_chat_now(chat_id: str, budget_inputs: Dict[str, Any]) -> ManualCompaction:
    """Compact through the shared coordinator, regardless of the trigger ratio.

    Runs as the post-turn phase: the caller has already checked that no run is
    live, so the persisted snapshot is the authoritative history — exactly the
    post-turn situation. No token limit is passed, so anything older than the
    verbatim tail is summarized; a history that fits in the tail is "nothing".
    The checkpoint sets ``notice_pending`` like any other, so the next turn
    emits the usual ``compaction_notice``.
    """
    if not config_settings.settings.compaction.enabled:
        return ManualCompaction(outcome="disabled")
    coordinator = CompactionCoordinator(chat_id)
    await coordinator.compact(phase=C.CompactionPhase.POST_TURN, budget_inputs=dict(budget_inputs))
    outcome = coordinator.outcome
    # The snapshot is also refused when no row is newer than the checkpoint
    # (an empty chat, or ``/compact`` twice in a row). That is nothing to do,
    # not a competing compactor.
    if outcome == "busy" and not await run_in_session(_lease_is_live, chat_id):
        outcome = "nothing"
    return ManualCompaction(
        outcome=outcome,
        tokens_before=coordinator.tokens_before,
        tokens_after=coordinator.tokens_after,
    )
