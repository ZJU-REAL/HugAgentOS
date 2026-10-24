"""Compose durable tool-effect execution for model runtime ports."""
from core.db.engine import SessionLocal
from core.llm.ports import register_tool_effect_runtime
from core.runs.tool_effects.errors import ToolEffectError, ToolIntentCommitError
from core.runs.tool_effects.gateway import ToolEffectGateway
from core.runs.tool_effects.journal import ToolEffectJournal
from core.runs.tool_effects.registry import DEFAULT_TOOL_RECOVERY_REGISTRY


class DurableToolEffects:
    def __init__(self, session_factory=None, registry=None):
        self.gateway = ToolEffectGateway(
            ToolEffectJournal(session_factory or SessionLocal),
            registry or DEFAULT_TOOL_RECOVERY_REGISTRY,
        )

    async def execute_outcome(self, **kwargs):
        return await self.gateway.execute_outcome(**kwargs)

    def binding_error(self, message):
        return ToolIntentCommitError(message)

    def response_error(self, message):
        return ToolEffectError(message)


def register():
    register_tool_effect_runtime(DurableToolEffects)
