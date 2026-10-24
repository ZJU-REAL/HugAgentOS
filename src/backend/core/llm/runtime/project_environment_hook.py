"""Recheck project authorization before the framework starts tool I/O."""
import asyncio
from fastapi import HTTPException
from core.harness.hooks import Decision, DecisionKind, HookSpec, HookStage
from core.llm.runtime.project_environment import validate_project_environment


def project_environment_hook(request):
    async def authorize(invocation):
        try:
            await asyncio.to_thread(validate_project_environment, request)
        except HTTPException as error:
            return Decision(DecisionKind.REJECT, reason=str(error.detail), patch={
                "tool_response": {"content": [{"type": "text", "text": str(error.detail)}],
                                  "state": "denied", "metadata": {"project_environment_denied": True}}})
        return Decision()
    return HookSpec("project_environment_authorization", HookStage.BEFORE_TOOL,
                    authorize, priority=50, timeout_ms=30000,
                    mutable_fields=frozenset({"tool_response"}))
