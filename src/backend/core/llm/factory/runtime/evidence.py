"""Agent assembly: evidence helpers. """

from __future__ import annotations

from typing import Any, Dict, Optional


def cache_compaction_execution_surface(agent: Any, base_prompt: str, surface: Any) -> None:
    """Mirror the exact surface used by the latest model request for compaction."""
    skill_instructions = str(getattr(surface, "skill_instructions", "") or "")
    agent._jx_compaction_system_prompt = (
        f"{base_prompt}\n{skill_instructions}" if skill_instructions else base_prompt
    )
    agent._jx_compaction_tool_schemas = getattr(surface, "tool_schemas", None)


def _default_allow_builtin_tools(
    *,
    channel_origin: Optional[Dict[str, Any]],
    automation_run: bool,
) -> bool:
    """Whether this trusted unattended entry point bypasses built-in policy."""
    return bool((channel_origin or {}).get("channel_id") or automation_run)
