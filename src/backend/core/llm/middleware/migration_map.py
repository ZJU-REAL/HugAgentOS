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
        (HookStage.BEFORE_RUN,),
        "neutral-hook",
        "removed; model selection runs through HookSpec",
    ),
    MiddlewareMigration(
        "FileContextMiddleware",
        (HookStage.TRANSFORM_CONTEXT,),
        "neutral-hook",
        "removed; attachments emit neutral context items",
    ),
    MiddlewareMigration(
        "SteerMiddleware",
        (HookStage.TRANSFORM_CONTEXT, HookStage.AFTER_CONTEXT, HookStage.BEFORE_TOOL),
        "neutral-hook",
        "removed; acknowledge only after context application",
    ),
    MiddlewareMigration(
        "WorkspacePinHintMiddleware",
        (HookStage.BEFORE_RUN, HookStage.TRANSFORM_CONTEXT),
        "neutral-hook",
        "removed; pin reminder emits neutral context items",
    ),
    MiddlewareMigration(
        "IterBudgetReminderMiddleware",
        (HookStage.TRANSFORM_CONTEXT,),
        "neutral-hook",
        "removed; budget context and tool choice are patched before binding",
    ),
    MiddlewareMigration(
        "StallInterventionMiddleware",
        (HookStage.BEFORE_TOOL, HookStage.AFTER_TOOL),
        "neutral-hook",
        "removed; evolution policy consumes neutral attempt history",
    ),
    MiddlewareMigration(
        "OntologyGateMiddleware",
        (HookStage.BEFORE_TOOL, HookStage.AFTER_TOOL),
        "neutral-hook",
        "removed; ontology admission returns reject/modify Decision",
    ),
    MiddlewareMigration(
        "ToolPermissionMiddleware",
        (HookStage.BEFORE_TOOL,),
        "neutral-hook",
        "removed; neutral admission policy owns tickets and execution scope",
    ),
    MiddlewareMigration(
        "CitationAnchorMiddleware",
        (HookStage.AFTER_TOOL,),
        "neutral-hook",
        "citation annotation returns tool_result patch",
    ),
    MiddlewareMigration(
        "ActingToolCallIdMiddleware",
        (HookStage.BEFORE_TOOL,),
        "neutral-hook",
        "tool Invocation carries call id",
    ),
    MiddlewareMigration(
        "ToolEffectMiddleware",
        (HookStage.BEFORE_TOOL, HookStage.AFTER_TOOL),
        "neutral-hook",
        "effect gateway is invoked by neutral tool runner",
    ),
    MiddlewareMigration(
        "JobLedgerReminderMiddleware",
        (HookStage.TRANSFORM_CONTEXT,),
        "neutral-hook",
        "removed; execution host contributes the ledger hook",
    ),
    MiddlewareMigration(
        "FinishPinGuardMiddleware",
        (HookStage.BEFORE_FINISH,),
        "neutral-hook",
        "finish guard returns an explicit Decision",
    ),
    MiddlewareMigration("CommandCompletionMiddleware",
                        (HookStage.TRANSFORM_CONTEXT, HookStage.BEFORE_FINISH),
                        "neutral-hook", "removed; completion decisions continue the active run"),
    MiddlewareMigration("ProjectEnvironmentMiddleware", (HookStage.BEFORE_TOOL,),
                        "neutral-hook", "removed; admission returns an explicit denial"),
    MiddlewareMigration("ScreenshotPruningMiddleware", (HookStage.TRANSFORM_CONTEXT,),
                        "neutral-hook", "removed; profile contributes a plain context transform"),
    MiddlewareMigration("PlanStaleReminderMiddleware", (HookStage.TRANSFORM_CONTEXT,),
                        "neutral-hook", "removed; plan reminder consumes a context snapshot"),
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
