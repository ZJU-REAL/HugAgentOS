"""Switches and size limits for call logging."""
from __future__ import annotations
import os

TOOL_LOG_ENABLED = os.getenv("TOOL_CALL_LOG_ENABLED", "true").lower() == "true"


SUBAGENT_LOG_ENABLED = os.getenv("SUBAGENT_LOG_ENABLED", "true").lower() == "true"


SKILL_LOG_ENABLED = os.getenv("SKILL_LOG_ENABLED", "true").lower() == "true"


SANDBOX_AUDIT_ENABLED = os.getenv("SANDBOX_AUDIT_ENABLED", "true").lower() == "true"


MAX_RESULT_BYTES = int(os.getenv("LOG_MAX_RESULT_BYTES", 64 * 1024))


MAX_STDOUT_BYTES = int(os.getenv("LOG_MAX_STDOUT_BYTES", 64 * 1024))


SANDBOX_TOOL_NAMES = {
    "Bash",
    "sandbox_put_artifact",
    "sandbox_get_artifact",
    "Write",
    "Edit",
    "Read",
    "Grep",
    "Glob",
}
