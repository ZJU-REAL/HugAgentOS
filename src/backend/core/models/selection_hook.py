"""Framework-neutral model choice before a run's context is assembled."""
from core.harness.hooks import Decision, FailureMode, HookSpec, HookStage, Invocation


def select_model(invocation: Invocation) -> Decision:
    runtime = invocation.data.get("runtime", {})
    if runtime.get("model_pinned"):
        return Decision()
    mode = runtime.get("chat_mode")
    if mode not in ("turbo", "fast", "low", "medium", "high", "xhigh", "max"):
        mode = "fast"
    return Decision.modify({"model_selection": {
        "provider_id": str(runtime.get("model_provider_id") or ""), "mode": mode,
    }})


def model_selection_hook() -> HookSpec:
    return HookSpec("model_selection", HookStage.BEFORE_RUN, select_model,
                    priority=10, failure_mode=FailureMode.OPEN,
                    mutable_fields=frozenset({"model_selection"}))
