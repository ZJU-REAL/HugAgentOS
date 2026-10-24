"""Translate model-selection decisions into the framework's model reference."""
import logging
from core.llm.runtime import model_cache
from core.models.usage import instrument_model_usage

logger = logging.getLogger(__name__)


def model_selection_input(agent):
    state = getattr(agent, "state", None)
    return {name: getattr(state, name, default) for name, default in (
        ("model_pinned", False), ("model_provider_id", ""), ("chat_mode", None),
    )}


def apply_model_selection(agent, invocation):
    selection = invocation.data.get("model_selection")
    if selection is None:
        return
    try:
        provider = selection["provider_id"]
        agent.model = (model_cache.get_provider_model(provider, mode=selection["mode"])
                       if provider else model_cache.get_main_model(mode=selection["mode"]))
        if invocation.run_id:
            instrument_model_usage(agent.model)
    except Exception as exc:
        # Model refresh is optional; retain the factory's existing model on error.
        logger.warning("[dynamic_model] failed: %s", exc)
