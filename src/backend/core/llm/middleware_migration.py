"""Auditable AgentScope middleware → neutral hook migration/deletion map."""

from __future__ import annotations

from dataclasses import dataclass

from core.harness.hooks import HookStage


@dataclass(frozen=True)
class MiddlewareMigration:
    middleware: str
    stages: tuple[HookStage, ...]
    status: str
    delete_after: str


MIDDLEWARE_MIGRATION: tuple[MiddlewareMigration, ...] = (
    MiddlewareMigration(
        "DynamicModelMiddleware",
        (HookStage.BEFORE_RUN, HookStage.BEFORE_MODEL),
        "adapter-covered",
        "neutral model-selection policy reaches parity",
    ),
    MiddlewareMigration(
        "FileContextMiddleware",
        (HookStage.TRANSFORM_CONTEXT,),
        "adapter-covered",
        "neutral attachment transformer reaches parity",
    ),
    MiddlewareMigration(
        "SteerMiddleware",
        (HookStage.BEFORE_MODEL, HookStage.BEFORE_TOOL),
        "adapter-covered",
        "durable steer policy consumes Invocation only",
    ),
    MiddlewareMigration(
        "WorkspacePinHintMiddleware",
        (HookStage.AFTER_TOOL, HookStage.BEFORE_MODEL),
        "adapter-covered",
        "pin reminder becomes a neutral context patch",
    ),
    MiddlewareMigration(
        "IterBudgetReminderMiddleware",
        (HookStage.BEFORE_MODEL,),
        "adapter-covered",
        "turn-budget reminder becomes a neutral context patch",
    ),
    MiddlewareMigration(
        "StallInterventionMiddleware",
        (HookStage.BEFORE_MODEL,),
        "adapter-covered",
        "stall policy consumes neutral attempt history",
    ),
    MiddlewareMigration(
        "OntologyGateMiddleware",
        (HookStage.BEFORE_TOOL, HookStage.AFTER_TOOL),
        "adapter-covered",
        "ontology decision returns reject/modify Decision",
    ),
    MiddlewareMigration(
        "CitationAnchorMiddleware",
        (HookStage.AFTER_TOOL,),
        "adapter-covered",
        "citation annotation returns tool_result patch",
    ),
    MiddlewareMigration(
        "ActingToolCallIdMiddleware",
        (HookStage.BEFORE_TOOL,),
        "adapter-covered",
        "tool Invocation carries call id",
    ),
    MiddlewareMigration(
        "ToolEffectMiddleware",
        (HookStage.BEFORE_TOOL, HookStage.AFTER_TOOL),
        "adapter-covered",
        "effect gateway is invoked by neutral tool runner",
    ),
    MiddlewareMigration(
        "JobLedgerReminderMiddleware",
        (HookStage.BEFORE_MODEL,),
        "adapter-covered",
        "job projection becomes a neutral context patch",
    ),
    MiddlewareMigration(
        "GoalAnchorReminderMiddleware",
        (HookStage.BEFORE_MODEL,),
        "adapter-covered",
        "goal reminder becomes a neutral context patch",
    ),
    MiddlewareMigration(
        "FinishPinGuardMiddleware",
        (HookStage.BEFORE_FINISH,),
        "adapter-covered",
        "finish guard returns an explicit Decision",
    ),
)


def deletion_checklist() -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "middleware": item.middleware,
            "stages": tuple(stage.value for stage in item.stages),
            "status": item.status,
            "delete_after": item.delete_after,
        }
        for item in MIDDLEWARE_MIGRATION
    )
