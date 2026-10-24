"""Cache model instances per chat mode and invalidate them on config changes."""
from __future__ import annotations
import threading
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

def serialized_model_cache(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with process_state().model_cache_lock:
            return function(*args, **kwargs)

    return locked


def check_version():
    """Invalidate cached model instances when ModelConfigService version changes."""
    from core.models.config import ModelConfigService

    current = ModelConfigService.get_instance().version
    if current != process_state().cached_version:
        process_state().model_cache = {}
        process_state().cached_version = current


def model_cache_key(name: str) -> tuple[Any, ...]:
    from core.harness.registry.host_runtime import requires_remote_execution

    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if not requires_remote_execution():
        return (name, loop, None)
    from core.harness.registry.execution_identity import state_fingerprint as _state_fingerprint, get_state

    identity = _state_fingerprint(get_state())
    # Eviction only releases this cache's reference. In-flight runs keep their
    # captured model and its existing request hook, which rejects identity changes.
    for key in list(process_state().model_cache):
        if isinstance(key, tuple) and key[0] == name and key[2] != identity:
            process_state().model_cache.pop(key, None)
    return name, loop, identity


@serialized_model_cache
def release_loop_models() -> None:
    import asyncio

    loop = asyncio.get_running_loop()
    for key in list(process_state().model_cache):
        if key[1] is loop:
            process_state().model_cache.pop(key, None)


@serialized_model_cache
def get_main_model(mode: str = "medium"):
    """Get the main agent model for the given chat mode (fast/medium/high/max)."""
    from core.models.chat_models import get_default_model
    from core.models.failover import with_failover
    from core.models.config import ModelConfigService

    check_version()
    key = model_cache_key(f"main:{mode}")
    cached = process_state().model_cache.get(key)
    if cached is not None:
        return cached
    if mode in ("fast", "turbo"):
        instance = get_default_model(disable_thinking=True, stream=True)
    elif mode in ("low", "high", "xhigh", "max"):
        instance = get_default_model(reasoning_effort=mode, stream=True)
    else:  # medium or unknown
        # medium uses the "thinking model", but picks chat_template_kwargs based on
        # supports_reasoning_effort (chat_models internally distinguishes
        # effort=medium vs None)
        instance = get_default_model(reasoning_effort="medium", stream=True)
    instance = with_failover(
        instance, ModelConfigService.get_instance().resolve("main_agent"), mode=mode
    )
    process_state().model_cache[key] = instance
    return instance


@serialized_model_cache
def get_provider_model(provider_id: str, mode: str = "medium"):
    """Get a user-selected active chat provider model for the given chat mode."""
    from core.models.chat_models import build_model_for_mode
    from core.models.failover import with_failover
    from core.models.config import ModelConfigService

    pid = (provider_id or "").strip()
    if not pid:
        return get_main_model(mode)
    check_version()
    key = model_cache_key(f"provider:{pid}:{mode}")
    cached = process_state().model_cache.get(key)
    if cached is not None:
        return cached
    resolved = ModelConfigService.get_instance().resolve_provider(pid)
    if resolved is None:
        return get_main_model(mode)

    instance = with_failover(
        build_model_for_mode(resolved, mode=mode, stream=True), resolved, mode=mode
    )
    process_state().model_cache[key] = instance
    return instance


def resolve_chat_mode(ctx) -> str:
    """Resolve the final chat_mode (turbo/fast/medium/high/max) from agent.state (AgentRuntimeState)."""
    raw = getattr(ctx, "chat_mode", None)
    if raw in ("turbo", "fast", "low", "medium", "high", "xhigh", "max"):
        return raw
    # Fallback for legacy clients
    return "medium" if getattr(ctx, "enable_thinking", True) else "fast"


@dataclass
class ModelCacheState:
    model_cache: dict[tuple[Any, ...], Any] = field(default_factory=lambda: {})
    cached_version: int = field(default_factory=lambda: -1)
    model_cache_lock: object = field(default_factory=lambda: threading.RLock())


_STATE = ModelCacheState()


def process_state() -> ModelCacheState:
    return _STATE
