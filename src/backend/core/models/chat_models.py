"""Build configured chat models without creating instances at import time."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from agentscope.credential import OpenAICredential
from agentscope.model import ChatModelBase, OpenAIChatModel, OpenAIResponseModel
from core.models.providers.openai_compat import OpenAICompatChatModel, ReasoningEchoChatFormatter, _build_chat_template_kwargs
from core.models.providers.registry import get_spec
from core.models.providers.vendor_models import build_litellm_model, build_native_model
from prompts.prompt_config import ModelConfig

logger = logging.getLogger(__name__)

# Shared per-event-loop, per-timeout httpx clients. A model instance is built
# fresh on every chat turn (``create_agent_executor`` → ``make_chat_model``), so
# a client constructed per instance meant a brand-new connection pool per turn:
# every turn paid a full TCP handshake to the gateway on the critical path
# before the first prefill byte moved (measured ~0.7-1.4s through a local TUN
# proxy, ~1 RTT on a clean link), and the client was never closed — sockets to
# the gateway grew monotonically (3 → 8 over three turns). Caching by
# (loop, timeout) keeps keep-alive connections warm across turns while staying
# correct under pytest-asyncio, which runs each test on its own loop and would
# otherwise inherit a pool bound to a closed one.


# Keep enough idle sockets for concurrent runs (main agent + subagents + memory
# extraction all talk to the same gateway) without letting an idle pool pin
# connections open forever against a gateway that culls them.
_POOL_LIMITS = httpx.Limits(
    max_connections=int(os.getenv("LLM_HTTP_MAX_CONNECTIONS", "100")),
    max_keepalive_connections=int(os.getenv("LLM_HTTP_MAX_KEEPALIVE", "40")),
    keepalive_expiry=float(os.getenv("LLM_HTTP_KEEPALIVE_EXPIRY_S", "300")),
)


async def close_loop_http_clients() -> None:
    """Release clients owned by a child loop before that loop is closed."""
    import asyncio

    loop_id = id(asyncio.get_running_loop())
    keys = [key for key in list(process_state().HTTP_CLIENTS) if key[0] == loop_id]
    for key in keys:
        client = process_state().HTTP_CLIENTS.pop(key)
        await client.aclose()


def _make_http_client(
    timeout: int, *, desktop_reference: str = "", base_url: str = ""
) -> httpx.AsyncClient:
    import asyncio

    from core.config.local_mode import install_local_network_tuning

    # The gateway culls idle connections well inside a person's typing pause, so
    # the next message reconnects and re-resolves. Cheap everywhere except on a
    # device whose resolver is slow, where it was the single largest cost in the
    # turn. No-op unless this process is the desktop local backend.
    install_local_network_tuning()

    base_t = float(timeout) if timeout else 600.0
    try:
        loop_key = id(asyncio.get_running_loop())
    except RuntimeError:
        # No running loop (sync construction in scripts/tests) — don't cache,
        # since we can't tell which loop will end up driving this client.
        loop_key = 0

    hooks = {}
    identity = ""
    if desktop_reference:
        from core.harness.registry.execution_identity import state_fingerprint as _state_fingerprint
        from core.harness.registry.model_credentials import request_hook

        hook, captured = request_hook(desktop_reference, base_url)
        identity = (desktop_reference, base_url, _state_fingerprint(captured))
        hooks = {"request": [hook]}
    key = (loop_key, base_t, identity)
    client = process_state().HTTP_CLIENTS.get(key)
    if client is not None and not client.is_closed:
        return client

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=base_t,
            read=base_t,
            write=base_t,
            pool=base_t,
        ),
        limits=_POOL_LIMITS,
        event_hooks=hooks,
    )
    if loop_key:
        process_state().HTTP_CLIENTS[key] = client
    return client


def _make_openai_compatible(
    spec,
    *,
    model: str,
    temperature: float,
    max_tokens: Optional[int],
    timeout: int,
    base_url: str,
    api_key: str,
    provider_extra: dict,
    disable_thinking: bool,
    reasoning_effort: str | int | None,
    stream: bool,
    context_size: int,
    structured_reasoning: Optional[bool] = None,
    reasoning_effort_is_wire: bool = False,
) -> OpenAICompatChatModel:
    azure: dict | None = None
    actual_model = model
    if spec.id == "azure_openai":
        azure = {"api_version": provider_extra.get("api_version", "")}
        actual_model = provider_extra.get("deployment") or model

    extra_body: dict[str, Any] = {
        "chat_template_kwargs": _build_chat_template_kwargs(
            disable_thinking=disable_thinking,
            reasoning_effort=reasoning_effort,
        )
    }
    if reasoning_effort_is_wire and reasoning_effort is not None:
        extra_body["chat_template_kwargs"] = {
            "thinking": True,
            "reasoning_effort": reasoning_effort,
        }
    if spec.reasoning_effort_top_level and reasoning_effort is not None:
        # OpenAI Responses-backed Chat Completions gateways expose reasoning
        # only when the effort is sent at the request root. Keep the nested
        # switch as well so OpenAI-compatible template controls still work.
        extra_body["reasoning_effort"] = reasoning_effort
    parameters = OpenAIChatModel.Parameters(
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return OpenAICompatChatModel(
        credential=OpenAICredential(
            api_key=api_key or "DUMMY",
            base_url=base_url or "https://api.openai.com/v1",
        ),
        model=actual_model or "dummy-model",
        parameters=parameters,
        stream=stream,
        http_client=_make_http_client(
            timeout,
            desktop_reference=(api_key if api_key.startswith("desktop-capability:") else ""),
            base_url=base_url,
        ),
        context_size=context_size,
        provider_id=spec.id,
        extra_body=extra_body,
        azure=azure,
        structured_reasoning=(
            spec.structured_reasoning if structured_reasoning is None else structured_reasoning
        ),
    )


def wants_responses(spec, api_protocol: Optional[str]) -> bool:
    """Whether this endpoint should be driven over ``/responses``.

    Public because the desktop model gateway has to route to the *same* path
    this module will call. When the two disagreed, every Responses-protocol
    model was proxied to ``/chat/completions`` and came back 404.

    Responses is the default: it carries reasoning as a first-class item instead of
    forcing it through ``reasoning_content``, so a thinking model's own reasoning can be
    handed back verbatim. Chat Completions is used only where the endpoint was found not
    to route ``/responses`` — a fact discovered at configuration time by
    ``protocol_probe`` and stored in ``extra_config.api_protocol``, never guessed here
    from the vendor name.
    """
    from core.models.providers.protocol_probe import PROTOCOL_CHAT, PROTOCOL_RESPONSES

    if not spec.speaks_responses:
        return False
    value = (api_protocol or "").strip().lower()
    if value == PROTOCOL_CHAT:
        return False
    if value == PROTOCOL_RESPONSES:
        return True
    # Unset: an endpoint nobody has probed yet. Default to Responses, which is what the
    # save-time probe and the startup backfill write for everything that supports it;
    # only endpoints positively found to lack the route are pinned to chat completions.
    return True


def _make_openai_responses(
    spec,
    *,
    model: str,
    temperature: float,
    max_tokens: Optional[int],
    timeout: int,
    base_url: str,
    api_key: str,
    disable_thinking: bool,
    reasoning_effort: str | int | None,
    stream: bool,
    context_size: int,
    reasoning_effort_is_wire: bool = False,
):
    """Build the Responses-protocol twin of ``_make_openai_compatible``."""
    from core.models.responses import OpenAICompatResponsesModel

    # The thinking switch keeps the same mechanism as the chat line: vLLM-style servers
    # read chat_template_kwargs on both protocols, so a model behaves identically no
    # matter which wire it is reached over.
    extra_body: dict[str, Any] = {
        "chat_template_kwargs": _build_chat_template_kwargs(
            disable_thinking=disable_thinking,
            reasoning_effort=reasoning_effort,
        )
    }
    # The effort deliberately does not go into Parameters: that field is a strict enum
    # of the API's own vocabulary, and the product's thinking levels are UI concepts
    # that do not all map onto it ("max" raises a validation error and the model cannot
    # be built at all). The model decides for itself which values the wire accepts.
    parameters = OpenAIResponseModel.Parameters(
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return OpenAICompatResponsesModel(
        credential=OpenAICredential(
            api_key=api_key or "DUMMY",
            base_url=base_url or "https://api.openai.com/v1",
        ),
        model=model or "dummy-model",
        parameters=parameters,
        stream=stream,
        http_client=_make_http_client(
            timeout,
            desktop_reference=(api_key if api_key.startswith("desktop-capability:") else ""),
            base_url=base_url,
        ),
        context_size=context_size,
        provider_id=spec.id,
        extra_body=extra_body,
        reasoning_effort=reasoning_effort,
        reasoning_effort_is_wire=reasoning_effort_is_wire,
    )


def make_chat_model(
    *,
    model: str,
    temperature: float,
    max_tokens: Optional[int],
    timeout: int,
    base_url: str,
    api_key: str,
    provider: str = "openai_compatible",
    provider_extra: Optional[dict] = None,
    disable_thinking: bool = False,
    reasoning_effort: str | int | None = None,
    stream: bool = False,
    context_size: Optional[int] = None,
    structured_reasoning: Optional[bool] = None,
    api_protocol: Optional[str] = None,
    reasoning_effort_is_wire: bool = False,
) -> ChatModelBase:
    """Construct a ChatModel dispatched by provider (AgentScope 2.0).

    When the provider is unknown or empty, falls back to openai_compatible (backward
    compatible with existing data).

    ``context_size`` (the model's real context window; AS2 compaction trigger threshold =
    trigger_ratio × context_size) has **no default fallback**:
    - Not passed (None) → resolve the real context_length from the Config admin model
      configuration by model name; if unconfigured a ``ValueError`` is raised, forcing the
      configuration to be completed;
    - Explicitly passed a positive number → use the resolved provider's configured window.
      Connectivity tests and the placeholder dummy model also pass their own window.
    """
    # Keep legacy provider/agent settings from restoring shorter stream timeouts.
    timeout = 600
    provider_extra = provider_extra or {}
    spec = get_spec(provider)

    if not context_size or context_size <= 0:
        from core.models.context_window import resolve_model_context_window

        context_size = resolve_model_context_window(model)

    if spec.engine == "native":
        return build_native_model(
            spec,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            base_url=base_url,
            api_key=api_key,
            context_size=context_size,
            stream=stream,
            timeout=timeout,
        )
    if spec.engine == "litellm":
        return build_litellm_model(
            spec,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            provider_extra=provider_extra,
            context_size=context_size,
            stream=stream,
        )
    # engine == "openai" (incl. azure_openai and all OpenAI-compatible vendor presets)
    if wants_responses(spec, api_protocol):
        return _make_openai_responses(
            spec,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            base_url=base_url,
            api_key=api_key,
            disable_thinking=disable_thinking,
            reasoning_effort=reasoning_effort,
            stream=stream,
            context_size=context_size,
            reasoning_effort_is_wire=reasoning_effort_is_wire,
        )
    return _make_openai_compatible(
        spec,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        base_url=base_url,
        api_key=api_key,
        provider_extra=provider_extra,
        disable_thinking=disable_thinking,
        reasoning_effort=reasoning_effort,
        stream=stream,
        context_size=context_size,
        structured_reasoning=structured_reasoning,
        reasoning_effort_is_wire=reasoning_effort_is_wire,
    )


def _resolve_or_dummy(role_key: str):
    """Resolve config from DB, return None if not available."""
    try:
        from core.models.config import ModelConfigService

        return ModelConfigService.get_instance().resolve(role_key)
    except Exception as exc:
        logger.warning("ModelConfigService unavailable for role '%s': %s", role_key, exc)
        return None


def build_model_for_mode(
    resolved, *, mode: Optional[str] = None, stream: bool = True
) -> ChatModelBase:
    """Build one chat model from a resolved provider config for a chat mode.

    The single place that turns a ``ResolvedModelConfig`` plus a chat mode into
    a model, so the primary endpoint and every failover candidate are built the
    same way rather than by three copies of the same flag arithmetic.
    """
    disable_thinking = mode in ("fast", "turbo")
    from core.models.reasoning_effort import resolve_reasoning_effort

    reasoning_effort = resolve_reasoning_effort(resolved.extra, mode)
    model = make_chat_model(
        model=resolved.model_name.replace("openai:", ""),
        temperature=resolved.temperature,
        max_tokens=resolved.max_tokens,
        timeout=resolved.timeout,
        base_url=resolved.base_url,
        api_key=resolved.api_key,
        provider=resolved.provider,
        provider_extra=resolved.provider_extra,
        disable_thinking=disable_thinking,
        reasoning_effort=reasoning_effort,
        context_size=resolved.context_length or None,
        stream=stream,
        structured_reasoning=(True if (resolved.extra or {}).get("structured_reasoning") else None),
        api_protocol=(resolved.extra or {}).get("api_protocol"),
        reasoning_effort_is_wire="reasoning_effort_levels" in (resolved.extra or {}),
    )

    model.resolved_model_provider_id = resolved.provider_id
    return model


def get_default_model(
    cfg: ModelConfig | None = None,
    disable_thinking: bool = False,
    reasoning_effort: str | int | None = None,
    stream: bool = False,
) -> ChatModelBase:
    cfg = cfg or ModelConfig()
    resolved = _resolve_or_dummy("main_agent")
    if resolved:
        return build_model_for_mode(
            resolved,
            mode="fast" if disable_thinking else reasoning_effort,
            stream=stream,
        )
    return make_chat_model(
        model="dummy-model",
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        timeout=cfg.timeout,
        base_url="",
        api_key="",
        disable_thinking=disable_thinking,
        reasoning_effort=reasoning_effort,
        stream=stream,
        # Placeholder dummy model: constructs successfully when the deployment has no model
        # configured, errors on invocation, never enters the compaction loop, and the value
        # participates in no computation; passed explicitly to bypass "resolve the real window
        # by model name" (which would inevitably fail).
        context_size=4096,
    )


def get_summarize_model(cfg: ModelConfig | None = None) -> ChatModelBase:
    cfg = cfg or ModelConfig()
    resolved = _resolve_or_dummy("summarizer")
    if resolved:
        # 2.0 OpenAIChatModel takes the bare model name; strip the 1.x "openai:" routing prefix.
        model_name = resolved.model_name.replace("openai:", "")
        return make_chat_model(
            model=model_name,
            temperature=resolved.temperature,
            max_tokens=resolved.max_tokens,
            timeout=resolved.timeout,
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            provider=resolved.provider,
            provider_extra=resolved.provider_extra,
            api_protocol=(resolved.extra or {}).get("api_protocol"),
        )
    return make_chat_model(
        model="dummy-model",
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        timeout=cfg.timeout,
        base_url="",
        api_key="",
        context_size=4096,  # dummy placeholder, same as above: participates in no computation
    )


__all__ = [
    "OpenAICompatChatModel",
    "ReasoningEchoChatFormatter",
    "make_chat_model",
    "build_model_for_mode",
    "get_default_model",
    "get_summarize_model",
    "close_loop_http_clients",
    "wants_responses",
]


@dataclass
class ChatModelsState:
    HTTP_CLIENTS: dict[tuple[int, float], httpx.AsyncClient] = field(default_factory=lambda: {})


_STATE = ChatModelsState()


def process_state() -> ChatModelsState:
    return _STATE
