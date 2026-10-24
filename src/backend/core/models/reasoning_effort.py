"""Per-model thinking levels, defaults and outbound parameter values.

Legacy rows retain their three product levels. Explicit mappings are authoritative:
a missing requested level uses this model's default, never a guessed provider value.
"""

from __future__ import annotations

from typing import Any

EFFORT_KEYS = ("low", "medium", "high", "xhigh", "max")
LEGACY_LEVELS = [{"key": key, "value": key} for key in ("medium", "high", "max")]


def validate_reasoning_config(extra: dict[str, Any]) -> str | None:
    levels = extra.get("reasoning_effort_levels")
    default = extra.get("default_reasoning_effort")
    if levels is None and default is None:
        return None
    if not extra.get("supports_reasoning_effort"):
        return "请先启用多档思考强度，再配置思考档位。"
    if not isinstance(levels, list) or not levels or len(levels) > len(EFFORT_KEYS):
        return "思考档位必须是非空列表，最多五档。"
    keys = set()
    for level in levels:
        if not isinstance(level, dict):
            return "每个思考档位必须包含 key 和 value。"
        key, value = level.get("key"), level.get("value")
        if key not in EFFORT_KEYS or key in keys:
            return "思考档位无效或重复。"
        keys.add(key)
        if isinstance(value, bool) or not (
            (isinstance(value, int) and value > 0)
            or (
                isinstance(value, str)
                and value.strip() == value
                and bool(value)
                and len(value) <= 64
            )
        ):
            return "思考参数必须是非空字符串或正整数。"
    if default not in keys:
        return "默认思考档位必须属于已启用的档位。"
    return None


def reasoning_capabilities(extra: dict[str, Any] | None) -> dict[str, Any]:
    extra = extra or {}
    enabled = bool(extra.get("supports_reasoning_effort"))
    levels = extra.get("reasoning_effort_levels")
    if levels is not None:
        error = validate_reasoning_config(extra)
        if error:
            raise ValueError(error)
    return {
        "supports_reasoning_effort": enabled,
        "reasoning_effort_levels": (
            levels
            if enabled and levels is not None
            else ([dict(level) for level in LEGACY_LEVELS] if enabled else [])
        ),
        "default_reasoning_effort": (
            extra.get("default_reasoning_effort", "medium") if enabled else "medium"
        ),
        "reasoning_effort_configured": levels is not None,
    }


def resolve_reasoning_effort(
    extra: dict[str, Any] | None, requested: str | None
) -> str | int | None:
    if requested in ("fast", "turbo"):
        return None
    caps = reasoning_capabilities(extra)
    if not caps["supports_reasoning_effort"]:
        return None
    if requested is None and not caps["reasoning_effort_configured"]:
        return None
    key = requested or caps["default_reasoning_effort"]
    mapping = {level["key"]: level["value"] for level in caps["reasoning_effort_levels"]}
    return mapping.get(key, mapping[caps["default_reasoning_effort"]])
