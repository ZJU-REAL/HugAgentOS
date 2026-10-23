"""Value types for tool intents, decisions and outcomes."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Mapping, Optional

@dataclass(frozen=True)
class ReconciliationResult:
    outcome: str
    result: Any = None

    @classmethod
    def applied(cls, result: Any) -> "ReconciliationResult":
        return cls("applied", result)

    @classmethod
    def not_applied(cls) -> "ReconciliationResult":
        return cls("not_applied")

    @classmethod
    def unknown(cls) -> "ReconciliationResult":
        return cls("unknown")


@dataclass(frozen=True)
class ToolPolicy:
    policy: str
    replayer: Optional[Callable[["ToolIntent"], Awaitable[Any] | Any]] = None
    reconciler: Optional[
        Callable[["ToolIntent"], Awaitable[ReconciliationResult | Any] | Any]
    ] = None


@dataclass(frozen=True)
class ToolIntent:
    effect_id: str
    result_id: str
    run_id: str
    operation_seq: int
    tool_name: str
    tool_call_id: str
    args_hash: str
    redacted_args: Mapping[str, Any]
    idempotency_key: str
    recovery_policy: str
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class IntentDecision:
    action: str
    intent: ToolIntent
    result: Any = None
    result_state: str = "success"

    @property
    def effect_id(self) -> str:
        return self.intent.effect_id


@dataclass(frozen=True)
class ToolExecutionOutcome:
    result: Any
    effect_id: str
    result_id: str
    result_state: str
    invoked: bool


@dataclass(frozen=True)
class RecoverySweepDecision:
    effect_id: str
    run_id: str
    action: str
