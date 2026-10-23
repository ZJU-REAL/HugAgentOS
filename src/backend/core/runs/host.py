"""Run services installed by the application composition root."""
from core.harness.registry.run_cancellation import register_run_cancellation
from core.runs import runtime


def register() -> None:
    from core.jobs.tool_operations import job_operations
    from core.harness.registry.job_operations import register_job_operations
    register_job_operations(job_operations)
    from core.chat.history_operations import history_operations
    from core.harness.registry.chat_history import register_chat_history
    register_chat_history(history_operations)
    from core.harness.registry.compaction import register_compaction_services
    from core.chat.compaction import service as compaction
    from core.chat.compaction.budget_checkpoint import ContextStopCheckpoint
    register_compaction_services(
        run=compaction.run_compaction, mid_turn=compaction.run_mid_turn_compaction,
        token_limit=compaction.resolve_token_limit, trigger_ratio=compaction.resolve_trigger_ratio,
        checkpoint=ContextStopCheckpoint,
    )
    from core.harness.registry.hook_factories import register_run_hook_factories
    from core.chat.steer_hook import SteeringHook
    from core.sandbox.commands.completion_hook import command_completion_hooks
    register_run_hook_factories(steering=SteeringHook, command_completion=command_completion_hooks)
    from core.harness.registry.hook_factories import register_hook_factory
    from core.jobs.reminder_hook import job_ledger_hooks
    register_hook_factory(job_ledger_hooks)
    from core.runs.tool_effects.hooks import tool_effect_hooks
    register_hook_factory(tool_effect_hooks)
    from core.llm.ports import register_execution_services
    from core.sandbox.commands.runtime import runtime as commands
    from core.sandbox.commands import store as records
    from core.jobs.agent import snapshot_parent_runtime
    register_execution_services(commands=commands, records=records, parent_snapshot=snapshot_parent_runtime)
    from core.chat.projection.codec import chat_projection_codec
    from core.runs.events.store import configure_projection
    configure_projection(chat_projection_codec())
    async def cancel(run_id: str, *, user_id: str, stop_terminal_worker: bool = False) -> bool:
        return await runtime.cancel_run(run_id, user_id=user_id, stop_terminal_worker=stop_terminal_worker)
    from core.runs.journal import RunJournal
    register_run_cancellation(cancel, cancel_in_session=RunJournal.cancel_in_session)
