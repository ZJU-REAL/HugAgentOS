"""Run ownership, receipts and recovery decisions shared by journal transactions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

LIVE_STATUSES = ("pending", "running")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
RECOVERABLE_PRE_MODEL_PHASES = ("accepted", "claimed", "pre_model")
RECOVERABLE_TOOL_PHASES = (
    "tool_intent_replay_safe",
    "tool_result_committed",
)
NON_CHAT_RUN_KINDS = ("plan_execute", "plan_generate", "autonomous_loop")
TOOL_BOUND_INTERNAL_RUN_KINDS = (
    "personal_agent_task",
    "automation_plan",
    "automation_prompt",
    "batch_item",
    "internal_job_agent",
    "legacy_chat_stream",
)
SAFE_STOP_ORPHAN_KINDS = (*TOOL_BOUND_INTERNAL_RUN_KINDS, "plan_generate")


class RunJournalError(RuntimeError):
    pass


class RunNotFound(RunJournalError):
    pass


class RunLeaseLost(RunJournalError):
    pass


class RunAlreadyExists(RunJournalError):
    """A caller-scoped run identity was already accepted durably."""


@dataclass(frozen=True)
class DurableRunBinding:
    run_id: str
    owner: str
    chat_id: str


@dataclass(frozen=True)
class JournalReceipt:
    run_id: str
    operation_seq: int
    snapshot_version: int
    phase: str


@dataclass(frozen=True)
class RecoveryDecision:
    run_id: str
    chat_id: str
    user_id: str
    message_id: str
    action: str
    phase: str
    snapshot: Mapping[str, Any]
    request_payload: Mapping[str, Any]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
