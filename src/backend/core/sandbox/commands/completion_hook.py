"""Expose terminal command facts at safe reasoning and finish boundaries."""
import asyncio
from core.harness.hooks import Decision, HookSpec, HookStage
from core.sandbox.commands import delivery, store


async def inject_completion(invocation):
    if invocation.operation_name != "agent_reasoning":
        return Decision()
    identity = invocation.data.get("identity", {})
    chat_id, user_id = identity.get("chat_id"), identity.get("user_id")
    if not (invocation.run_id and chat_id and user_id):
        return Decision()
    if not await asyncio.to_thread(store.has_pending, chat_id, user_id):
        return Decision()
    rows = await asyncio.to_thread(delivery.reserve_boundary, chat_id, user_id, invocation.run_id)
    if not rows:
        return Decision()
    item = dict(content=delivery.notice_text(rows), kind="reminder",
                origin="harness:command_completion", trust="system", priority=950,
                token_budget=4000, truncation_policy="never")
    return Decision.modify({"context_items": [*invocation.data.get("context_items", ()), item]})


async def pending_completion(invocation):
    identity = invocation.data.get("identity", {})
    chat_id, user_id = identity.get("chat_id"), identity.get("user_id")
    if not (invocation.run_id and chat_id and user_id):
        return Decision()
    rows = await asyncio.to_thread(store.pending_notices, chat_id)
    if any(row["notice_status"] == "pending" and row["user_id"] == user_id for row in rows):
        return Decision.modify({"continue_run": True})
    return Decision()


def command_completion_hooks():
    return (
        HookSpec("command_completion_context", HookStage.TRANSFORM_CONTEXT,
                 inject_completion, priority=90, timeout_ms=30000,
                 mutable_fields=frozenset({"context_items"})),
        HookSpec("command_completion_finish", HookStage.BEFORE_FINISH,
                 pending_completion, priority=90, timeout_ms=30000,
                 mutable_fields=frozenset({"continue_run"})),
    )
