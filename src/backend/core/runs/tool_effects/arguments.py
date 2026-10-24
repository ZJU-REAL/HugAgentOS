"""Canonicalise and redact tool arguments and derive idempotency keys."""
from __future__ import annotations
import hashlib
import json
from typing import Any, Mapping, Optional
from core.infra.data_masking import mask_sensitive_data
from core.runs.tool_effects import errors
from core.runs.tool_effects import journal_constants

def embedded_redact(value: str) -> str:
    cleaned = value
    for pattern in journal_constants.EMBEDDED_SECRET_PATTERNS:
        cleaned = pattern.sub(r"\1[REDACTED]", cleaned)
    return cleaned


def sanitize_value(value: Any, *, key: str = "") -> Any:
    lower = key.lower()
    if isinstance(value, Mapping):
        return {str(k): sanitize_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item, key=key) for item in value]
    if isinstance(value, str):
        if lower in journal_constants.WITHHOLD_FIELDS:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            return {"_withheld": True, "sha256": digest, "length": len(value)}
        return embedded_redact(value)
    return value


def canonical_tool_args(args: Mapping[str, Any]) -> tuple[str, str, Mapping[str, Any]]:
    canonical = json.dumps(
        args or {}, sort_keys=True, separators=(",", ":"), default=str
    )
    args_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    by_field = mask_sensitive_data(dict(args or {}), field_patterns=journal_constants.REDACT_FIELDS)
    return canonical, args_hash, sanitize_value(by_field)


def tool_args_hash(args: Mapping[str, Any]) -> str:
    return canonical_tool_args(args)[1]


def stable_idempotency_key(run_id: str, tool_name: str, invocation_id: str) -> str:
    raw = f"{run_id}\x00{tool_name}\x00{invocation_id}".encode("utf-8")
    return "tool:" + hashlib.sha256(raw).hexdigest()


def resolved_idempotency_key(
    *,
    run_id: str,
    principal_scope: str,
    tool_name: str,
    tool_call_id: str,
    explicit_key: Optional[str],
) -> tuple[str, bool]:
    caller_key = str(explicit_key or "").strip()
    if caller_key:
        scoped = f"{principal_scope}\x00{caller_key}".encode("utf-8")
        return "caller:" + hashlib.sha256(scoped).hexdigest(), True
    if not tool_call_id:
        raise errors.ToolIntentCommitError(
            "tool_call_id or explicit idempotency_key is required"
        )
    return stable_idempotency_key(run_id, tool_name, tool_call_id), False
