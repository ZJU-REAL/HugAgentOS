"""Authoritative user-facing context-usage snapshots.

The provider's usage object is the only exact token counter.  The canonical
request manifest supplies a deterministic fallback and an explainable category
mix, but its byte-based tokenizer must never be presented as provider truth.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

SCHEMA_VERSION = "context-usage.v1"
BREAKDOWN_KEYS = ("messages", "tools", "thinking", "files", "system", "input")
_SYSTEM_KINDS = {
    "system_rule",
    "identity",
    "project_material",
    "reference",
    "memory",
    "reminder",
}
_TOOL_KINDS = {"tool_call", "tool_result"}


def _non_negative(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _empty_breakdown() -> Dict[str, int]:
    return {key: 0 for key in BREAKDOWN_KEYS}


def _manifest_breakdown(manifest: Mapping[str, Any]) -> Dict[str, int]:
    breakdown = _empty_breakdown()
    included = manifest.get("included")
    if not isinstance(included, list):
        included = []
    for item in included:
        if not isinstance(item, Mapping) or item.get("visibility") == "manifest_only":
            continue
        tokens = _non_negative(item.get("final_tokens", item.get("token_estimate")))
        kind = str(item.get("kind") or "")
        if kind in _TOOL_KINDS:
            breakdown["tools"] += tokens
        elif kind == "attachment":
            breakdown["files"] += tokens
        elif kind in _SYSTEM_KINDS:
            breakdown["system"] += tokens
        else:
            breakdown["messages"] += tokens

    details = manifest.get("budget_details")
    if isinstance(details, Mapping):
        breakdown["tools"] += _non_negative(details.get("tool_reserve_tokens"))
    return breakdown


def _reconcile_breakdown(
    breakdown: Mapping[str, Any],
    target: int,
) -> Dict[str, int]:
    """Scale estimated categories so their integer sum equals an exact total."""
    target = _non_negative(target)
    raw = {key: _non_negative(breakdown.get(key)) for key in BREAKDOWN_KEYS}
    raw_total = sum(raw.values())
    if target == 0:
        return _empty_breakdown()
    if raw_total == 0:
        raw["system"] = target
        return raw

    scaled = {key: (raw[key] * target) / raw_total for key in BREAKDOWN_KEYS}
    result = {key: math.floor(scaled[key]) for key in BREAKDOWN_KEYS}
    remainder = target - sum(result.values())
    order = sorted(
        BREAKDOWN_KEYS,
        key=lambda key: (scaled[key] - result[key], raw[key], key),
        reverse=True,
    )
    for key in order[:remainder]:
        result[key] += 1
    return result


def _agent_metadata(agent: Any, manifest: Mapping[str, Any]) -> tuple[int, str, str]:
    model = getattr(agent, "model", None)
    state = getattr(agent, "state", None)
    details = manifest.get("budget_details")
    context_window = (
        _non_negative(details.get("context_window"))
        if isinstance(details, Mapping)
        else 0
    )
    if context_window <= 0:
        context_window = _non_negative(getattr(model, "context_size", 0))
    model_name = str(
        getattr(model, "model", None)
        or getattr(model, "model_name", None)
        or getattr(state, "model_name", None)
        or ""
    )
    provider_id = str(getattr(state, "model_provider_id", "") or "")
    return context_window, model_name, provider_id


def build_context_usage_snapshot(
    agent: Any,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    model_call_index: int,
) -> Dict[str, Any]:
    """Build one snapshot for the latest primary model call.

    A positive provider prompt count makes the total exact.  Category values are
    still a composition estimate, reconciled to that exact total so the UI can
    never display a breakdown whose parts disagree with the headline.
    """
    manifest = getattr(agent, "request_context_manifest", None)
    manifest = manifest if isinstance(manifest, Mapping) else {}
    context_window, model_name, provider_id = _agent_metadata(agent, manifest)
    prompt = _non_negative(prompt_tokens)
    completion = _non_negative(completion_tokens)
    exact = prompt > 0
    breakdown = _manifest_breakdown(manifest)

    if exact:
        prompt_breakdown = _reconcile_breakdown(breakdown, prompt)
    else:
        prompt = sum(breakdown.values())
        prompt_breakdown = breakdown
    prompt_breakdown["messages"] += completion
    used = prompt + completion

    return {
        "schema_version": SCHEMA_VERSION,
        "source": "provider" if exact else "backend_estimate",
        "exact": exact,
        "used_tokens": used,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "context_window": context_window,
        "model_name": model_name,
        "model_provider_id": provider_id,
        "model_call_index": max(0, int(model_call_index or 0)),
        "breakdown": prompt_breakdown,
    }


def build_compaction_context_usage(
    estimate: Mapping[str, Any],
    *,
    context_window: int,
    model_name: str = "",
    model_provider_id: str = "",
) -> Dict[str, Any]:
    """Project the post-compaction prompt until the provider measures it."""
    system = _non_negative(estimate.get("system_prompt_tokens"))
    tools = _non_negative(estimate.get("tool_schema_tokens"))
    messages = _non_negative(estimate.get("message_tokens"))
    overhead = _non_negative(estimate.get("provider_overhead_tokens"))
    breakdown = _empty_breakdown()
    breakdown.update(
        {
            "messages": messages,
            "tools": tools,
            # Provider framing is not a user message or tool definition.  Keep
            # it in the system bucket so every token remains accounted for.
            "system": system + overhead,
        }
    )
    used = sum(breakdown.values())
    return {
        "schema_version": SCHEMA_VERSION,
        "source": "compaction_estimate",
        "exact": False,
        "used_tokens": used,
        "prompt_tokens": used,
        "completion_tokens": 0,
        "context_window": _non_negative(context_window),
        "model_name": str(model_name or ""),
        "model_provider_id": str(model_provider_id or ""),
        "model_call_index": 0,
        "breakdown": breakdown,
    }


def latest_persisted_context_usage(chat_service: Any, chat_id: str) -> Optional[Dict[str, Any]]:
    """Return the newest assistant snapshot without loading the whole chat."""
    rows = chat_service.message_repo.list_recent_by_chat(chat_id, limit=50)
    for row in reversed(rows):
        extra = getattr(row, "extra_data", None)
        raw = extra.get("context_usage") if isinstance(extra, Mapping) else None
        if isinstance(raw, Mapping) and raw.get("schema_version") == SCHEMA_VERSION:
            return dict(raw)
    return None


__all__ = [
    "BREAKDOWN_KEYS",
    "SCHEMA_VERSION",
    "build_compaction_context_usage",
    "build_context_usage_snapshot",
    "latest_persisted_context_usage",
]
