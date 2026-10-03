"""Durable memory outbox: payloads and dependency configuration."""

from __future__ import annotations

from core.config.settings import settings
from core.db.engine import SessionLocal

__all__ = ["settings", "SessionLocal"]
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from core.memory.context import MemoryContext
from core.memory.extractors.router import ExtractorType

logger = logging.getLogger(__name__)

_TERMINAL = {"succeeded", "quarantined"}
_LAYER_BY_EXTRACTOR = {
    ExtractorType.IDENTITY: "L1:identity",
    ExtractorType.PREFERENCE: "L1:preference",
    ExtractorType.PROCEDURAL: "L2:procedural",
    ExtractorType.GRAPH: "L3:graph",
    ExtractorType.TASK: "session:task",
}


class RetryableMemoryError(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _context_payload(ctx: MemoryContext) -> dict[str, Any]:
    return {
        "user_id": ctx.user_id,
        "workspace_id": ctx.workspace_id,
        "chat_id": ctx.chat_id,
        "allowed_levels": list(ctx.allowed_levels),
        "confidentiality": ctx.confidentiality,
        "actor": ctx.actor,
        "write_enabled": ctx.write_enabled,
        "scope_user_id": ctx.scope_user_id,
        "message_id": ctx.message_id,
        "effect_id": ctx.effect_id,
    }


def _context_from_payload(payload: dict[str, Any]) -> MemoryContext:
    return MemoryContext(
        user_id=str(payload.get("user_id") or ""),
        workspace_id=str(payload.get("workspace_id") or "default"),
        chat_id=payload.get("chat_id"),
        allowed_levels=tuple(payload.get("allowed_levels") or ("public", "internal", "sensitive")),
        confidentiality=payload.get("confidentiality"),
        actor=payload.get("actor"),
        write_enabled=bool(payload.get("write_enabled")),
        scope_user_id=payload.get("scope_user_id"),
        message_id=payload.get("message_id"),
        effect_id=payload.get("effect_id"),
    )


def _effective_message_id(ctx: MemoryContext, candidate_hash: str) -> str:
    if ctx.message_id:
        return str(ctx.message_id)
    scope = ctx.chat_id or ctx.user_id or "anonymous"
    return f"legacy:{scope}:{candidate_hash[:24]}"


def _scope_key(ctx: MemoryContext) -> str:
    return _stable_hash(
        {
            "scope_user_id": ctx.effective_scope_user_id,
            "workspace_id": ctx.workspace_id or "default",
        }
    )


def _settlement_hash(message_id: str) -> str:
    return _stable_hash({"message_id": message_id, "kind": "settlement"})
