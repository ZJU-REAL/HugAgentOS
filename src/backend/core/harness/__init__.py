"""Framework-neutral harness contracts.

Nothing in this package imports an agent framework. Framework adapters live in
``core.llm`` and translate their native objects into these stable contracts.
"""

from core.harness.events import Event, EventSink
from core.harness.hooks import (
    Decision,
    DecisionKind,
    FailureMode,
    HookBus,
    HookContractError,
    HookPaused,
    HookRejected,
    HookSpec,
    HookStage,
    Invocation,
    find_hook_paused,
)
from core.harness.usage import AttemptUsage, MemoryUsageRecorder, UsageAttempt

__all__ = [
    "AttemptUsage",
    "Decision",
    "DecisionKind",
    "Event",
    "EventSink",
    "FailureMode",
    "HookBus",
    "HookContractError",
    "HookPaused",
    "HookRejected",
    "HookSpec",
    "HookStage",
    "Invocation",
    "MemoryUsageRecorder",
    "UsageAttempt",
    "find_hook_paused",
]
