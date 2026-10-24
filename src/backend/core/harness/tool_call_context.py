"""Execution identity shared by nested tool calls and run entry points."""
from contextvars import ContextVar

CURRENT_TOOL_CALL_ID: ContextVar[str] = ContextVar("jx_current_tool_call_id", default="")
CURRENT_RUN_BINDING: ContextVar[tuple[str, str] | None] = ContextVar("jx_current_run_binding", default=None)
