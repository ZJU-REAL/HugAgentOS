"""Write sandbox tool calls as security audit events."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Dict, Optional
from sqlalchemy.orm import Session
from core.db.engine import SessionLocal
from core.infra.logging import get_logger
from core.harness import log_context

logger = get_logger(__name__)

async def resolve_sandbox_id(session_id: Optional[str]) -> Optional[str]:
    """Look up the current sandbox instance id from the sandbox provider by session key (default = chat_id). Pure query; None on failure."""
    if not session_id:
        return None
    try:
        from core.sandbox import get_sandbox_provider

        return await get_sandbox_provider().current_sandbox_id(session_id)
    except (
        Exception
    ):  # noqa: BLE001 — audit enrichment is best-effort, must never affect the main path
        return None


def audit_status(raw: Optional[str]) -> str:
    return AUDIT_STATUS_MAP.get(raw or "success", "failure")


def insert_sandbox_audit(
    *,
    action: str,
    sandbox_id: Any,
    user_id: Optional[str],
    trace_id: Optional[str],
    details: Dict[str, Any],
    status: str,
    created_at: Optional[datetime],
) -> None:
    """Write one resource=sandbox security audit row. Best-effort: failure only degrades to a log entry, never affects the main path."""
    from core.db.repository import AuditLogRepository

    db: Session = SessionLocal()
    try:
        # Reuse the audit repository's write (has its own add/commit/rollback; audit failure doesn't block the main path).
        AuditLogRepository(db).create(
            {
                "user_id": user_id
                or None,  # empty string would trip the FK constraint; normalize to NULL
                "action": action,
                "resource_type": "sandbox",
                "resource_id": str(sandbox_id)[:64],
                "sandbox_id": str(sandbox_id)[:128],
                "details": details,
                "trace_id": trace_id,
                "status": status,
                "created_at": created_at or log_context.now_utc(),
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("sandbox_audit_write_failed", error=str(exc), sandbox=str(sandbox_id))
    finally:
        db.close()


def write_sandbox_audit_sync(record: Dict[str, Any]) -> None:
    """Write one sandbox-type tool call as a security audit event (resource=sandbox / sandbox instance id).

    Separate from tool-call logging: audits let "Security Management → Audit
    Logs" distinguish and filter by sandbox instance. details only summarizes
    the input args (redacted + truncated), never dumps full results.
    """
    sandbox_id = record.get("sandbox_id")
    if not sandbox_id:
        return
    tool_name = record.get("tool_name")
    action = SANDBOX_AUDIT_ACTIONS.get(tool_name, "sandbox.tool.call")
    args_safe = log_context.sanitize_tool_args(record.get("tool_args"))
    details: Dict[str, Any] = {"tool": tool_name}
    if isinstance(args_safe, dict):
        cmd = args_safe.get("command")
        if cmd is not None:
            preview, _ = log_context.truncate_text(str(cmd), 2000)
            details["command"] = preview
        # File/search tools: record path and pattern (not large fields like content) to trace what was changed/read.
        for k in ("file_path", "filename", "path", "pattern", "artifact_id", "name"):
            if args_safe.get(k) is not None:
                details[k] = args_safe[k]
    if record.get("error_message"):
        details["error"], _ = log_context.truncate_text(str(record["error_message"]), 500)

    insert_sandbox_audit(
        action=action,
        sandbox_id=sandbox_id,
        user_id=record.get("user_id"),
        trace_id=record.get("trace_id"),
        details=details,
        status=audit_status(record.get("status")),
        created_at=record.get("created_at"),
    )


def write_skill_sandbox_audit_sync(record: Dict[str, Any]) -> None:
    """Skill script execution (run_script) = running code in the sandbox; write one sandbox.skill.run audit.

    Triggered only by run_script (view / auto_load do not execute code in the
    sandbox); skipped when there is no sandbox instance.
    """
    sandbox_id = record.get("sandbox_id")
    if not sandbox_id:
        return
    details: Dict[str, Any] = {
        "skill_id": record.get("skill_id"),
        "skill_name": record.get("skill_name"),
        "script_name": record.get("script_name"),
        "script_language": record.get("script_language"),
    }
    if record.get("error_message"):
        details["error"], _ = log_context.truncate_text(str(record["error_message"]), 500)

    insert_sandbox_audit(
        action="sandbox.skill.run",
        sandbox_id=sandbox_id,
        user_id=record.get("user_id"),
        trace_id=record.get("trace_id"),
        details=details,
        status=audit_status(record.get("status")),
        created_at=record.get("created_at"),
    )


SANDBOX_AUDIT_ACTIONS = {
    "Bash": "sandbox.bash.exec",
    "sandbox_put_artifact": "sandbox.artifact.put",
    "sandbox_get_artifact": "sandbox.artifact.get",
    "Write": "sandbox.file.write",
    "Edit": "sandbox.file.edit",
    "Read": "sandbox.file.read",
    "Grep": "sandbox.file.grep",
    "Glob": "sandbox.file.glob",
}


AUDIT_STATUS_MAP = {"success": "success", "error": "error"}
