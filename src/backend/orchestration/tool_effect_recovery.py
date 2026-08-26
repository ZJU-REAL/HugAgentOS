"""Production recovery adapters for durable tool effects.

Replay bypasses the model and invokes the exact toolkit adapter recorded by
the Intent. Reconciliation adapters inspect the external/local authority first
and only permit replay when they can prove the effect was not applied.
"""

from __future__ import annotations

import json
from typing import Any

from agentscope.message import ToolCallBlock
from agentscope.tool._response import ToolResponse

from core.db.engine import SessionLocal
from core.db.models import ChatRun, ToolEffectReceipt
from core.services.tool_effect_ledger import ReconciliationResult, ToolEffectError, ToolIntent


def _contains_withheld(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value.get("_withheld")) or any(
            _contains_withheld(item) for item in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_withheld(item) for item in value)
    return False


def _plain_replay_args(intent: ToolIntent) -> dict[str, Any]:
    args = dict(intent.redacted_args or {})
    if _contains_withheld(args):
        raise ToolEffectError("replay arguments contain withheld sensitive free text")
    if "[REDACTED" in json.dumps(args, ensure_ascii=False, default=str):
        raise ToolEffectError("replay arguments were redacted and cannot be reconstructed safely")
    return args


async def replay_tool_intent(intent: ToolIntent) -> dict[str, Any]:
    """Rebuild the frozen chat tool surface and invoke one adapter, without an LLM call."""

    from core.llm.agent_factory import create_agent_executor
    from core.llm.mcp_manager import close_clients

    with SessionLocal() as db:
        run = db.get(ChatRun, intent.run_id)
        if run is None:
            raise ToolEffectError(f"missing run for tool recovery: {intent.run_id}")
        snapshot = dict(run.recovery_snapshot or {})
        worker_args = dict(snapshot.get("worker_args") or {})
        context = dict(worker_args.get("context") or {})
        user_id = run.user_id
        chat_id = run.chat_id

    enabled_skill_ids = context.get("skill_ids") or context.get("enabled_skill_ids")
    enabled_mcp_ids = context.get("mcp_ids") or context.get("enabled_mcp_ids")
    enabled_kb_ids = context.get("kb_ids") or context.get("enabled_kb_ids")
    agent = None
    clients = []
    try:
        agent, clients = await create_agent_executor(
            enabled_skill_ids=list(enabled_skill_ids or []),
            enabled_mcp_ids=list(enabled_mcp_ids or []),
            enabled_kb_ids=list(enabled_kb_ids or []),
            current_user_id=user_id,
            reranker_enabled=bool(context.get("reranker_enabled", False)),
            model_name=context.get("model_name"),
            model_provider_id=context.get("model_provider_id"),
            chat_mode=context.get("chat_mode"),
            memory_enabled=bool(context.get("memory_enabled", False)),
            chat_id=chat_id,
            run_id=intent.run_id,
            project_ctx=context.get("project_ctx"),
            channel_origin=context.get("channel_origin"),
            automation_run=bool(context.get("automation_run")),
            ontology_runtime=context.get("ontology_runtime"),
        )
        state = agent.state
        state.apply_request_context(
            {
                **context,
                "user_id": user_id,
                "chat_id": chat_id,
                "run_id": intent.run_id,
            },
            "",
        )
        replay_args = _plain_replay_args(intent)
        if intent.tool_name in {
            "create_scheduled_task",
            "update_scheduled_task",
            "delete_scheduled_task",
        }:
            replay_args["tool_effect_id"] = intent.effect_id
        tool_call = ToolCallBlock(
            id=intent.tool_call_id or f"recovery-{intent.effect_id}",
            name=intent.tool_name,
            input=json.dumps(replay_args, ensure_ascii=False),
        )
        final = None
        async for item in agent.toolkit.call_tool(tool_call, state):
            if isinstance(item, ToolResponse):
                final = item
        if final is None:
            raise ToolEffectError(f"recovery adapter returned no ToolResponse: {intent.tool_name}")
        return {"tool_response": final.model_dump(mode="json")}
    finally:
        if clients:
            await close_clients(clients)


async def reconcile_scheduled_task_intent(intent: ToolIntent) -> ReconciliationResult:
    """Use the adapter receipt committed in the same transaction as the mutation."""

    _plain_replay_args(intent)
    with SessionLocal() as db:
        run = db.get(ChatRun, intent.run_id)
        if run is None:
            return ReconciliationResult.unknown()
        receipt = db.get(ToolEffectReceipt, intent.effect_id)
        if receipt is None:
            return ReconciliationResult.not_applied()
        if receipt.user_id != run.user_id or receipt.tool_name != intent.tool_name:
            return ReconciliationResult.unknown()
        return ReconciliationResult.applied(dict(receipt.result_payload or {}))


__all__ = ["reconcile_scheduled_task_intent", "replay_tool_intent"]
