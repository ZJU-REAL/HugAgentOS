"""Recovery policy for each tool; unknown tools are never replayed."""
from __future__ import annotations
from typing import Any, Awaitable, Callable, Optional, cast
from core.runs.tool_effects import types

RECOVERY_POLICIES = ("replay_safe", "reconcile", "never_replay")


class ToolRecoveryRegistry:
    """Explicit adapter contracts. Unknown tools fail closed as never-replay."""

    def __init__(self) -> None:
        self._policies: dict[str, types.ToolPolicy] = {}

    def register(
        self,
        tool_name: str,
        policy: str,
        *,
        replayer: Optional[Callable[[types.ToolIntent], Awaitable[Any] | Any]] = None,
        reconciler: Optional[
            Callable[[types.ToolIntent], Awaitable[types.ReconciliationResult | Any] | Any]
        ] = None,
    ) -> None:
        if policy not in RECOVERY_POLICIES:
            raise ValueError(f"invalid recovery policy: {policy}")
        if policy == "replay_safe" and reconciler is not None:
            raise ValueError("replay_safe does not accept a reconciler")
        if policy == "never_replay" and (
            replayer is not None or reconciler is not None
        ):
            raise ValueError("never_replay does not accept recovery adapters")
        self._policies[str(tool_name)] = types.ToolPolicy(policy, replayer, reconciler)

    def resolve(self, tool_name: str) -> types.ToolPolicy:
        return self._policies.get(str(tool_name), types.ToolPolicy("never_replay"))


async def default_replayer(intent: types.ToolIntent) -> Any:
    from core.runs.tool_effect_recovery import replay_tool_intent

    return await replay_tool_intent(intent)


async def scheduled_task_reconciler(intent: types.ToolIntent) -> types.ReconciliationResult:
    from core.runs.tool_effect_recovery import reconcile_scheduled_task_intent

    return cast(types.ReconciliationResult, await reconcile_scheduled_task_intent(intent))


def build_default_tool_recovery_registry() -> ToolRecoveryRegistry:
    registry = ToolRecoveryRegistry()
    for name in (
        "Read",
        "read_project_instructions",
        "list_sites",
        "Glob",
        "Grep",
        "view_text_file",
        "get_data_context",
        "read_image",
        "read_artifact",
        "channel_read_attachment",
        "sandbox_get_artifact",
    ):
        registry.register(name, "replay_safe", replayer=default_replayer)
    for name in (
        "Write",
        "save_project_instructions",
        "Edit",
        "Bash",
        "sandbox_put_artifact",
        "pin_to_workspace",
        "run_job",
        "choose_design",
        "call_subagent",
        "update_plan",
        "load_plugin",
    ):
        registry.register(name, "never_replay")
    for name in (
        "create_scheduled_task",
        "update_scheduled_task",
        "delete_scheduled_task",
    ):
        registry.register(
            name,
            "reconcile",
            replayer=default_replayer,
            reconciler=scheduled_task_reconciler,
        )
    return registry


DEFAULT_TOOL_RECOVERY_REGISTRY = build_default_tool_recovery_registry()
