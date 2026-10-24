"""Errors raised by the tool effect ledger."""
from __future__ import annotations
from typing import Optional

class ToolEffectError(RuntimeError):
    pass


class ToolIntentCommitError(ToolEffectError):
    """The intent could not be durably committed; invocation is forbidden."""


class ToolOutcomeUnknown(ToolEffectError):
    """The ledger proves intent but cannot prove the external outcome."""


def find_tool_outcome_unknown(exc: BaseException) -> Optional[ToolOutcomeUnknown]:
    """Unwrap AgentScope/anyio ExceptionGroup layers without losing safety meaning."""
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ToolOutcomeUnknown):
            return current
        nested = getattr(current, "exceptions", None)
        if isinstance(nested, tuple):
            pending.extend(item for item in nested if isinstance(item, BaseException))
        if isinstance(current.__cause__, BaseException):
            pending.append(current.__cause__)
        if isinstance(current.__context__, BaseException):
            pending.append(current.__context__)
    return None


class ToolEffectLeaseLost(ToolEffectError):
    pass


class ToolIdempotencyConflict(ToolEffectError):
    """A caller reused one explicit key for different logical input."""
