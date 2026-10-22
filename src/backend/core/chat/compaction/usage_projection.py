"""Select one current context snapshot across model calls and checkpoints."""

from __future__ import annotations

from datetime import timezone
from typing import Any, Dict

from core.llm.context.usage import SCHEMA_VERSION, build_compaction_context_usage
from core.config import settings as config_settings
from core.llm.context import budget as compaction


def get_chat_context_state(chat_service: Any, chat_id: str) -> Dict[str, Any]:
    """Return consistent API projections; a completed compaction supersedes usage.

    Message sequence alone cannot order mid-turn compression and later model
    calls: both belong to the same assistant row. New snapshots carry their
    capture time. Legacy snapshots retain the sequence fallback.
    """
    rows = chat_service.message_repo.list_recent_by_chat(chat_id, limit=50, metadata_only=True)
    usage = None
    usage_seq = 0
    for row in reversed(rows):
        extra = getattr(row, "extra_data", None)
        raw = extra.get("context_usage") if isinstance(extra, dict) else None
        if isinstance(raw, dict) and raw.get("schema_version") == SCHEMA_VERSION:
            usage = dict(raw)
            usage_seq = int(getattr(row, "chat_seq", 0) or 0)
            usage["chat_seq"] = usage_seq
            break

    checkpoint = (
        chat_service.get_latest_compaction_checkpoint(chat_id)
        if config_settings.settings.compaction.enabled
        else None
    )
    if checkpoint is None:
        return {"context_usage": usage, "context_compaction": None}
    extra = getattr(checkpoint, "extra_data", None) or {}
    covered_seq = chat_service._checkpoint_covered_seq(checkpoint)
    replacement = extra.get("replacement_history")
    replacement = replacement if isinstance(replacement, list) else []
    state = {
        "checkpoint_id": getattr(checkpoint, "message_id", ""),
        "covered_through_message_id": extra.get("covers_up_to_message_id"),
        "covered_through_chat_seq": covered_seq,
        "covered_message_count": chat_service.message_repo.count_visible_through_seq(
            chat_id, covered_seq
        ),
        "replacement_tokens": compaction.estimate_history_tokens(replacement),
    }
    created = getattr(checkpoint, "created_at", None)
    # SQLite returns naive UTC timestamps. Never interpret them in host time.
    if created and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    checkpoint_ms = int(created.timestamp() * 1000) if created else 0
    state["checkpoint_created_at"] = created.isoformat() if created else None
    captured = int((usage or {}).get("captured_at_ms") or 0)
    newer_usage = usage is not None and (
        captured > checkpoint_ms if captured and checkpoint_ms else usage_seq > covered_seq
    )
    if newer_usage:
        return {"context_usage": usage, "context_compaction": state}

    manifest = extra.get("replacement_manifest") or {}
    replacement_usage = manifest.get("replacement_context_usage")
    if not isinstance(replacement_usage, dict):
        # Old checkpoints have no frozen instruction surface. Estimate only
        # their replacement, retaining attribution from the previous call.
        estimate = compaction.estimate_context_budget(
            messages=extra.get("replacement_history") or []
        )
        replacement_usage = build_compaction_context_usage(
            estimate,
            context_window=int((usage or {}).get("context_window") or 0),
            model_name=str((usage or {}).get("model_name") or ""),
            model_provider_id=str((usage or {}).get("model_provider_id") or ""),
        )
    replacement_usage = {
        **replacement_usage,
        "captured_at_ms": checkpoint_ms,
        "chat_seq": covered_seq,
        "checkpoint_id": state["checkpoint_id"],
    }
    # Pre-turn checkpoints predating attribution fields can still reuse the
    # last known window; it is explicitly an estimate until the next call.
    for key in ("context_window", "model_name", "model_provider_id"):
        if not replacement_usage.get(key) and usage:
            replacement_usage[key] = usage.get(key)
    state["context_usage"] = replacement_usage
    return {"context_usage": replacement_usage, "context_compaction": state}
