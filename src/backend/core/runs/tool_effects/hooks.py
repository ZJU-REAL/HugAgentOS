"""Durable tool admission is an execution-layer policy."""
from core.harness.hooks import Decision, HookSpec, HookStage


def require_durable_tool_binding(invocation):
    identity = invocation.data.get("identity", {})
    if not invocation.run_id or not identity.get("journal_owner") or not invocation.operation_name:
        return Decision.reject("tool execution requires a durable run_id and journal_owner binding")
    return Decision()


def tool_effect_hooks(request):
    yield HookSpec("durable_tool_binding", HookStage.BEFORE_TOOL,
                   require_durable_tool_binding, priority=800)
