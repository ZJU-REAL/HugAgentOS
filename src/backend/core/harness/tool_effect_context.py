"""Invocation identity shared by tool execution and transport adapters."""
from __future__ import annotations
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class ToolInvocationContext:
    effect_id: str
    result_id: str
    idempotency_key: str
    run_id: str
    fencing_owner: str


CURRENT_TOOL_EFFECT: ContextVar[Optional[ToolInvocationContext]] = ContextVar(
    "jx_current_tool_effect", default=None
)
