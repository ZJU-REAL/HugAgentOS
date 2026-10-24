"""Read the frozen model and prompt surface a turn's compaction budget is computed from."""

from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

import core.llm.context.compacting_agent as compacting_agent
import core.models.context_window as context_manager

logger = logging.getLogger(__name__)


def _resolve_agent_model_runtime(agent: Any, fallback_model_name: Any = "") -> Tuple[str, int]:
    """Return the effective model name and context window baked into an agent.

    AgentScope chat models expose the upstream name as ``.model``. Some provider
    wrappers expose ``.model_name`` instead, so accept both before consulting the
    request fallback. Channel runs intentionally omit a model selection; converting
    that ``None`` to the literal string ``"None"`` previously made direct sub-agent
    conversations fail context-window lookup even though the resolved main model was
    correctly configured.
    """
    model = getattr(agent, "model", None)
    state = getattr(agent, "state", None)
    candidates = (
        getattr(model, "model", None),
        getattr(model, "model_name", None),
        getattr(state, "model_name", None),
        fallback_model_name,
    )
    model_name = ""
    for candidate in candidates:
        text = str(candidate or "").strip()
        if text and text.lower() != "none":
            model_name = text
            break

    context_window = int(getattr(model, "context_size", 0) or 0)
    if context_window <= 0:
        context_window = context_manager.resolve_model_context_window(model_name)
    return model_name, context_window


def _compaction_budget_inputs(agent: Any, context_window: int) -> Dict[str, Any]:
    """Read the exact frozen prompt/tool surface cached by agent_factory."""
    system_prompt, tool_schema = compacting_agent.frozen_prompt_surface(agent)
    return {
        "system_prompt": system_prompt,
        "tool_schema": tool_schema,
        "context_window": int(context_window or 0),
        "model_name": str(
            getattr(getattr(agent, "model", None), "model", None)
            or getattr(getattr(agent, "state", None), "model_name", None)
            or ""
        ),
        "model_provider_id": str(
            getattr(getattr(agent, "model", None), "resolved_model_provider_id", "")
            or getattr(getattr(agent, "state", None), "model_provider_id", "")
            or ""
        ),
    }


def _pre_turn_compaction_inputs(
    agent: Any,
    context_window: int,
    resolved_model_name: str,
) -> Dict[str, Any]:
    """Adapt the shared budget snapshot to the pre-turn service signature."""
    inputs = _compaction_budget_inputs(agent, context_window)
    inputs.pop("model_provider_id", None)
    inputs["model_name"] = str(inputs.get("model_name") or resolved_model_name)
    return inputs
