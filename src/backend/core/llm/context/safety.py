"""Final request admission and a task-local stop latch."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from core.harness.hooks import HookPaused
from core.models.context_window import usable_context_window


class ContextBudgetExceeded(HookPaused):
    def __init__(self, *, manifest: dict, context: list, summary: str):
        self.manifest = manifest
        self.context = context
        self.summary = summary
        self.saved = False
        super().__init__(
            "context_budget_exceeded: "
            f"used={manifest['used_tokens']} budget={manifest['total_budget']}"
        )


@dataclass
class RequestAdmission:
    stopped: ContextBudgetExceeded | None = None
    context: dict | None = None
    checkpoint: Any = None

    def check(self) -> None:
        if self.stopped is not None:
            raise self.stopped


_admission: ContextVar[RequestAdmission | None] = ContextVar("request_admission", default=None)


@contextmanager
def request_admission_scope(*, context=None, checkpoint=None):
    admission = RequestAdmission(context=context, checkpoint=checkpoint)
    from core.models.admission import request_admission_callback
    token = _admission.set(admission)
    try:
        with request_admission_callback(admission.check):
            yield admission
    finally:
        _admission.reset(token)


async def enforce_context_budget(agent: Any, assembly: Any = None) -> None:
    admission = _admission.get()
    if admission is not None:
        admission.check()
    previous = getattr(agent, "_context_budget_stop", None)
    if previous is not None:
        raise previous
    if assembly is None:
        return
    details = assembly.manifest.get("budget_details") or {}
    schemas_over_budget = details.get("tool_reserve_tokens", 0) > usable_context_window(
        details.get("context_window", 0)
    )
    if not assembly.over_budget and not schemas_over_budget:
        return
    state = agent.state
    stopped = ContextBudgetExceeded(
        manifest=dict(assembly.manifest),
        context=[message.model_dump(mode="json") for message in state.context],
        summary=str(state.summary or ""),
    )
    agent._context_budget_stop = stopped
    if admission is not None:
        admission.stopped = stopped
    raise stopped


def remember_context_output(*, message_id: str, steps: list, content: str) -> None:
    admission = _admission.get()
    if admission is not None:
        from core.harness.registry.compaction import create_context_checkpoint

        admission.checkpoint = create_context_checkpoint(
            message_id=message_id,
            steps=list(steps),
            content=content,
        )
