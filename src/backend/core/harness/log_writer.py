"""Async entry points that schedule call-log writes off the event loop."""
from __future__ import annotations
import asyncio
from typing import Any, Dict, List, Optional
from core.infra.background import spawn
from core.infra.logging import get_logger
from core.harness import log_audit
from core.harness import log_context
from core.harness import log_records
from core.harness import log_writer_constants

logger = get_logger(__name__)

async def write_tool_call(record: Dict[str, Any]) -> None:
    record.setdefault("source", log_context.current_source())
    record.setdefault("subagent_log_id", log_context.current_subagent_log_id())
    ctx = log_context.context_ids()
    record.setdefault("trace_id", ctx["trace_id"])
    record.setdefault("user_id", ctx["user_id"])
    record.setdefault("chat_id", ctx["chat_id"])
    record.setdefault("message_id", log_context.current_message_id.get())
    # Sandbox-type tools: resolve the sandbox instance id they run in (session
    # key default = chat_id). Awaiting in place on this async path — provider
    # reads its active-session table (synchronous in-memory read, no side
    # effects) — is more reliable than passing a ContextVar across tasks.
    if record.get("tool_name") in log_writer_constants.SANDBOX_TOOL_NAMES:
        if not record.get("sandbox_id"):
            record["sandbox_id"] = await log_audit.resolve_sandbox_id(record.get("chat_id"))
        # Security audit is independent of the tool-call log switch: write the audit first, then the (optional) tool log.
        if log_writer_constants.SANDBOX_AUDIT_ENABLED:
            try:
                await asyncio.to_thread(log_audit.write_sandbox_audit_sync, record)
            except Exception as exc:  # noqa: BLE001
                logger.warning("sandbox_audit_task_failed", error=str(exc))
    if not log_writer_constants.TOOL_LOG_ENABLED:
        return
    try:
        await asyncio.to_thread(log_records.write_tool_call_sync, record)
    except Exception as exc:  # noqa: BLE001
        logger.warning("tool_call_log_task_failed", error=str(exc))


async def start_subagent_log(record: Dict[str, Any]) -> str:
    log_id = record.get("id") or log_context.new_id()
    record["id"] = log_id
    if not log_writer_constants.SUBAGENT_LOG_ENABLED:
        return log_id
    ctx = log_context.context_ids()
    record.setdefault("trace_id", ctx["trace_id"])
    record.setdefault("user_id", ctx["user_id"])
    record.setdefault("chat_id", ctx["chat_id"])
    record.setdefault("message_id", log_context.current_message_id.get())
    record.setdefault("parent_subagent_log_id", log_context.current_subagent_log_id())
    record.setdefault("started_at", log_context.now_utc())
    try:
        await asyncio.to_thread(log_records.write_subagent_create_sync, record)
    except Exception as exc:  # noqa: BLE001
        logger.warning("subagent_log_start_failed", error=str(exc))
    return log_id


async def finish_subagent_log(
    subagent_log_id: str,
    *,
    status: str = "success",
    output_content: Optional[str] = None,
    intermediate_steps: Optional[List[Dict[str, Any]]] = None,
    token_usage: Optional[Dict[str, int]] = None,
    tool_calls_count: int = 0,
    skill_calls_count: int = 0,
    error_message: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    if not log_writer_constants.SUBAGENT_LOG_ENABLED or not subagent_log_id:
        return
    updates = {
        "output_content": output_content,
        "intermediate_steps": intermediate_steps,
        "token_usage": token_usage,
        "tool_calls_count": tool_calls_count,
        "skill_calls_count": skill_calls_count,
        "error_message": error_message,
        "duration_ms": duration_ms,
    }
    try:
        await asyncio.to_thread(log_records.write_subagent_finish_sync, subagent_log_id, status, updates)
    except Exception as exc:  # noqa: BLE001
        logger.warning("subagent_log_finish_failed", error=str(exc))


async def write_skill_call(record: Dict[str, Any]) -> str:
    log_id = record.get("id") or log_context.new_id()
    record["id"] = log_id
    ctx = log_context.context_ids()
    record.setdefault("trace_id", ctx["trace_id"])
    record.setdefault("user_id", ctx["user_id"])
    record.setdefault("chat_id", ctx["chat_id"])
    record.setdefault("message_id", log_context.current_message_id.get())
    # Skill script execution (run_script) = running code in the sandbox → write a security audit (independent of the skill log switch).
    if log_writer_constants.SANDBOX_AUDIT_ENABLED and record.get("invocation_type") == "run_script":
        if not record.get("sandbox_id"):
            record["sandbox_id"] = await log_audit.resolve_sandbox_id(record.get("chat_id"))
        try:
            await asyncio.to_thread(log_audit.write_skill_sandbox_audit_sync, record)
        except Exception as exc:  # noqa: BLE001
            logger.warning("skill_sandbox_audit_task_failed", error=str(exc))
    if not log_writer_constants.SKILL_LOG_ENABLED:
        return log_id
    try:
        await asyncio.to_thread(log_records.write_skill_call_sync, record)
    except Exception as exc:  # noqa: BLE001
        logger.warning("skill_call_log_task_failed", error=str(exc))
    return log_id


def schedule_tool_call_write(record: Dict[str, Any]) -> None:
    spawn(write_tool_call(record), name="log_writer.tool_call")


def schedule_skill_call_write(record: Dict[str, Any]) -> None:
    spawn(write_skill_call(record), name="log_writer.skill_call")
