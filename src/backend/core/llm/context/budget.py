"""Estimate the model-facing context components for compression."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core.llm.context import compaction as C


def estimate_history_tokens(history: List[Dict[str, Any]]) -> int:
    """Estimate history text and image reserves without DB or LLM calls."""
    return sum(C.estimate_content_tokens(m.get("content")) for m in history)


def estimate_context_budget(
    *,
    system_prompt: str = "",
    tool_schema: Any = None,
    messages: Optional[List[Dict[str, Any]]] = None,
    provider_overhead_tokens: Optional[int] = None,
    context_window: int = 0,
    model_name: str = "",
    model_provider_id: str = "",
) -> Dict[str, int]:
    """Return one auditable estimate shared by trigger decisions and manifests.

    The estimate covers exactly what the request will send — system prompt, tool
    schemas, history and protocol overhead. Reserving the model's ``max_tokens``
    on top of that is deliberately absent: output headroom is carried once, as
    the window share in ``EFFECTIVE_CONTEXT_WINDOW_PERCENT``, so a per-model
    output cap the operator never configured cannot move the trigger line.
    """
    # These identity fields travel with the frozen budget inputs so the
    # post-compaction gauge retains the same model/window attribution. They do
    # not participate in token arithmetic.
    del context_window, model_name, model_provider_id
    history = messages or []
    serialized_tools = (
        json.dumps(tool_schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if tool_schema
        else ""
    )
    system_tokens = C.approx_token_count(system_prompt or "")
    tool_tokens = C.approx_token_count(serialized_tools)
    message_tokens = estimate_history_tokens(history)
    overhead_tokens = (
        max(0, int(provider_overhead_tokens))
        if provider_overhead_tokens is not None
        else 8 + (3 * len(history))
    )
    return {
        "system_prompt_tokens": system_tokens,
        "tool_schema_tokens": tool_tokens,
        "message_tokens": message_tokens,
        "provider_overhead_tokens": overhead_tokens,
        "total_estimated_tokens": (system_tokens + tool_tokens + message_tokens + overhead_tokens),
    }


def should_compact(active_tokens: Optional[int], limit: Optional[int]) -> bool:
    from core.config.settings import settings

    if not settings.compaction.enabled:
        return False
    if not active_tokens or not limit or limit <= 0:
        return False
    return active_tokens >= limit


def resolve_snapshot_active_tokens(snapshot: Any, inputs: Dict[str, Any], estimate: int) -> int:
    """Prefer a measured final assistant row still outside the checkpoint.

    Older measurements cannot describe a rewritten or already compacted tail.
    Matching the frozen provider and window prevents another model's usage
    from overriding this run's estimate.
    """
    rows = snapshot.source_rows
    if not rows:
        return estimate
    row = rows[-1]
    usage = (row.extra_data or {}).get("context_usage")
    if (
        row.role != "assistant"
        or row.chat_seq != snapshot.covered_seq
        or not isinstance(usage, dict)
        or usage.get("schema_version") != "context-usage.v2"
        or usage.get("source") != "provider"
        or usage.get("exact") is not True
    ):
        return estimate
    for key in ("model_provider_id", "model_name", "context_window"):
        if not inputs.get(key) or usage.get(key) != inputs[key]:
            return estimate
    tokens = usage.get("used_tokens")
    captured = usage.get("captured_at_ms")
    if type(tokens) is not int or tokens <= 0 or type(captured) is not int or captured <= 0:
        return estimate
    return tokens
