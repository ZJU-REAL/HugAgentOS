"""Synchronous writers for tool, sub-agent and skill call logs."""
from __future__ import annotations
from typing import Any, Dict
from sqlalchemy.orm import Session
from core.db.engine import SessionLocal
from core.db.models import SkillCallLog, SubAgentCallLog, ToolCallLog, ToolEffectLedger
from core.infra.data_masking import mask_sensitive_data
from core.infra.logging import get_logger
from core.harness import log_context
from core.harness import log_writer_constants

logger = get_logger(__name__)

def write_tool_call_sync(record: Dict[str, Any]) -> None:
    if not log_writer_constants.TOOL_LOG_ENABLED:
        return
    db: Session = SessionLocal()
    try:
        args_safe = log_context.sanitize_tool_args(record.get("tool_args"))
        args_safe, _ = log_context.truncate_json(args_safe, log_writer_constants.MAX_RESULT_BYTES)
        result_safe, result_trunc = log_context.truncate_json(record.get("tool_result"), log_writer_constants.MAX_RESULT_BYTES)
        username = log_context.fetch_username(db, record.get("user_id", ""))
        effect = None
        effect_id = str(record.get("effect_id") or "")
        run_id = str(record.get("run_id") or "")
        tool_call_id = str(record.get("tool_call_id") or "")
        if effect_id:
            effect = (
                db.query(ToolEffectLedger)
                .filter(
                    ToolEffectLedger.effect_id == effect_id,
                    ToolEffectLedger.event_type == "intent",
                )
                .one_or_none()
            )
        if effect is None and run_id and tool_call_id:
            effect = (
                db.query(ToolEffectLedger)
                .filter(
                    ToolEffectLedger.run_id == run_id,
                    ToolEffectLedger.tool_call_id == tool_call_id,
                    ToolEffectLedger.event_type == "intent",
                )
                .order_by(ToolEffectLedger.event_id.desc())
                .first()
            )
        # ToolCallLog stays a replaceable query projection. The pre-allocated
        # result id makes retries update the same row, while ToolEffectLedger
        # remains the append-only recovery authority.
        row_id = (
            (effect.result_id if effect is not None else None)
            or record.get("result_id")
            or record.get("id")
            or log_context.new_id()
        )
        row = db.get(ToolCallLog, row_id) or ToolCallLog(id=row_id)
        row.effect_id = effect.effect_id if effect is not None else record.get("effect_id")
        row.trace_id = record.get("trace_id")
        row.chat_id = record.get("chat_id")
        row.message_id = record.get("message_id")
        row.user_id = record.get("user_id")
        row.user_name = username
        row.tool_name = record["tool_name"]
        row.tool_display_name = record.get("tool_display_name")
        row.tool_call_id = record.get("tool_call_id")
        row.mcp_server = record.get("mcp_server")
        row.tool_args = args_safe
        row.tool_result = result_safe
        row.result_truncated = bool(result_trunc)
        # A top-level error payload is a failed projection even when an MCP
        # adapter declared the transport call successful.
        row.status = log_context.effective_status(record.get("status"), record.get("tool_result"))
        row.error_message = record.get("error_message")
        row.duration_ms = record.get("duration_ms")
        row.source = record.get("source") or "main_agent"
        row.subagent_log_id = record.get("subagent_log_id")
        row.skill_log_id = record.get("skill_log_id")
        row.sandbox_id = record.get("sandbox_id")
        row.started_at = record.get("started_at")
        row.created_at = record.get("created_at") or log_context.now_utc()
        db.add(row)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("tool_call_log_write_failed", error=str(exc), tool=record.get("tool_name"))
    finally:
        db.close()


def write_subagent_create_sync(record: Dict[str, Any]) -> None:
    if not log_writer_constants.SUBAGENT_LOG_ENABLED:
        return
    db: Session = SessionLocal()
    try:
        input_safe, _ = log_context.truncate_json(record.get("input_messages"), log_writer_constants.MAX_RESULT_BYTES)
        username = log_context.fetch_username(db, record.get("user_id", ""))
        row = SubAgentCallLog(
            id=record["id"],
            trace_id=record.get("trace_id"),
            chat_id=record.get("chat_id"),
            message_id=record.get("message_id"),
            user_id=record.get("user_id"),
            user_name=username,
            subagent_id=record.get("subagent_id"),
            subagent_name=record["subagent_name"],
            subagent_type=record.get("subagent_type"),
            plan_id=record.get("plan_id"),
            step_id=record.get("step_id"),
            step_index=record.get("step_index"),
            step_title=record.get("step_title"),
            model=record.get("model"),
            input_messages=input_safe,
            status="running",
            parent_subagent_log_id=record.get("parent_subagent_log_id"),
            started_at=record.get("started_at") or log_context.now_utc(),
            created_at=log_context.now_utc(),
        )
        db.add(row)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning(
            "subagent_log_create_failed", error=str(exc), name=record.get("subagent_name")
        )
    finally:
        db.close()


def write_subagent_finish_sync(
    subagent_log_id: str,
    status: str,
    updates: Dict[str, Any],
) -> None:
    if not log_writer_constants.SUBAGENT_LOG_ENABLED:
        return
    db: Session = SessionLocal()
    try:
        row = db.query(SubAgentCallLog).filter(SubAgentCallLog.id == subagent_log_id).one_or_none()
        if row is None:
            return
        row.status = status
        if "output_content" in updates:
            output, _ = log_context.truncate_text(updates.get("output_content"), log_writer_constants.MAX_RESULT_BYTES)
            row.output_content = output
        if "intermediate_steps" in updates:
            steps_safe, _ = log_context.truncate_json(updates.get("intermediate_steps"), log_writer_constants.MAX_RESULT_BYTES)
            row.intermediate_steps = steps_safe
        if "token_usage" in updates:
            row.token_usage = updates.get("token_usage")
        if "tool_calls_count" in updates:
            row.tool_calls_count = updates.get("tool_calls_count")
        if "skill_calls_count" in updates:
            row.skill_calls_count = updates.get("skill_calls_count")
        if "error_message" in updates:
            row.error_message = updates.get("error_message")
        if "duration_ms" in updates:
            row.duration_ms = updates.get("duration_ms")
        row.completed_at = log_context.now_utc()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("subagent_log_finish_failed", error=str(exc), id=subagent_log_id)
    finally:
        db.close()


def write_skill_call_sync(record: Dict[str, Any]) -> None:
    if not log_writer_constants.SKILL_LOG_ENABLED:
        return
    db: Session = SessionLocal()
    try:
        args_safe = mask_sensitive_data(record.get("script_args"), field_patterns=log_context.REDACT_FIELDS)
        args_safe, _ = log_context.truncate_json(args_safe, log_writer_constants.MAX_RESULT_BYTES)
        stdout_safe, trunc_out = log_context.truncate_text(record.get("script_stdout"), log_writer_constants.MAX_STDOUT_BYTES)
        stderr_safe, trunc_err = log_context.truncate_text(record.get("script_stderr"), log_writer_constants.MAX_STDOUT_BYTES)
        stdin_safe, _ = log_context.truncate_text(record.get("script_stdin"), log_writer_constants.MAX_STDOUT_BYTES)
        username = log_context.fetch_username(db, record.get("user_id", ""))
        row = SkillCallLog(
            id=record.get("id") or log_context.new_id(),
            trace_id=record.get("trace_id"),
            chat_id=record.get("chat_id"),
            message_id=record.get("message_id"),
            user_id=record.get("user_id"),
            user_name=username,
            skill_id=record["skill_id"],
            skill_name=record.get("skill_name"),
            skill_version=record.get("skill_version"),
            skill_source=record.get("skill_source"),
            invocation_type=record.get("invocation_type") or "auto_load",
            script_name=record.get("script_name"),
            script_language=record.get("script_language"),
            script_args=args_safe,
            script_stdin=stdin_safe,
            script_stdout=stdout_safe,
            script_stderr=stderr_safe,
            output_truncated=bool(trunc_out or trunc_err),
            exit_code=record.get("exit_code"),
            status=record.get("status") or "success",
            error_message=record.get("error_message"),
            duration_ms=record.get("duration_ms"),
            source=record.get("source") or log_context.current_source(),
            subagent_log_id=record.get("subagent_log_id") or log_context.current_subagent_log_id(),
            started_at=record.get("started_at"),
            created_at=record.get("created_at") or log_context.now_utc(),
        )
        db.add(row)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("skill_call_log_write_failed", error=str(exc), skill=record.get("skill_id"))
    finally:
        db.close()
