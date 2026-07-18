"""Model factory utilities (AgentScope 2.0 backend) — multi-vendor dispatch.

Important: do NOT construct model instances at import time.
This keeps the FastAPI app importable even when the DB has no rows.

All model configuration is resolved from the DB via ModelConfigService.

Dispatches by provider (vendor) to three engine kinds (see core/llm/providers/registry.py):
  - openai : OpenAI-compatible (incl. domestic compatible-vendor presets + Azure OpenAI) → OpenAICompatChatModel
  - native : AgentScope native classes (Anthropic / Gemini / DashScope / Ollama)
  - litellm: adapted via litellm (Bedrock etc.)

Two hard requirements for subclassing ``OpenAIChatModel`` (OpenAI-compatible path only):
  1. During long tool_call generation a single chunk can go silent for 130-160s → must keep the
     read=600s httpx timeout (see STREAM_READ_TIMEOUT_S). Done by injecting a custom
     ``httpx.AsyncClient``.
  2. Qwen/minimax go through OpenAI-compat, where the thinking-chain switch lives in
     ``extra_body.chat_template_kwargs`` rather than OpenAI-native reasoning_effort. Done by
     injecting extra_body into every call.

The L3 placeholder-summary fallback for failed compaction calls is provided uniformly by
``providers._fallback.StructuredFallbackMixin``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, AsyncGenerator, Optional

import httpx
from agentscope.credential import OpenAICredential
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import Msg
from agentscope.model import ChatModelBase, ChatResponse, OpenAIChatModel
from agentscope.tool._types import ToolChoice

from prompts.prompt_config import ModelConfig

from core.llm.providers._fallback import L3_SYNTHETIC_METADATA, StructuredFallbackMixin  # noqa: F401
from core.llm.providers.registry import get_spec, split_provider_extra
from core.llm.providers.vendor_models import build_litellm_model, build_native_model

logger = logging.getLogger(__name__)


# See the 1.x comment: read timeout raised separately to 600s, leaving ample time for long tool_call args generation.
STREAM_READ_TIMEOUT_S: float = 600.0


def _build_chat_template_kwargs(
    *,
    disable_thinking: bool,
    reasoning_effort: Optional[str],
) -> dict:
    """Build chat_template_kwargs (Qwen/minimax thinking-chain switch, via extra_body)."""
    if disable_thinking:
        return {"enable_thinking": False}
    if reasoning_effort is None:
        return {"enable_thinking": True}
    if reasoning_effort == "medium":
        return {"thinking": True}
    return {"thinking": True, "reasoning_effort": reasoning_effort}


class OpenAICompatChatModel(StructuredFallbackMixin, OpenAIChatModel):
    """OpenAIChatModel subclass: injects a custom http_client + extra_body; optional Azure OpenAI client.

    Pinned to agentscope==2.0.0: the ``_call_api`` body is copied from the parent class (2.0.0);
    sync it when upgrading upstream. The L3 compaction fallback is provided by StructuredFallbackMixin.
    """

    def __init__(
        self,
        *,
        credential: OpenAICredential,
        model: str,
        parameters: "OpenAIChatModel.Parameters",
        stream: bool,
        http_client: httpx.AsyncClient,
        # Required, no default: AS2 uses context_size to compute the compaction trigger threshold
        # (trigger_ratio × context_size). We once silently inherited the upstream 128000 default,
        # causing a real 256k model to repeatedly trigger compaction at half its window.
        # The source of truth is context_length from the Config admin model configuration
        # (resolved inside make_chat_model).
        context_size: int,
        extra_body: dict | None = None,
        azure: dict | None = None,
    ) -> None:
        super().__init__(
            credential=credential,
            model=model,
            parameters=parameters,
            stream=stream,
            # max_retries=0: let the agent layer (ModelConfig.max_retries) own retries
            # exclusively, avoiding the retry multiplication of documented risk 7 (worst case 24 attempts).
            max_retries=0,
            context_size=context_size,
            formatter=OpenAIChatFormatter(),
        )
        self._http_client = http_client
        self._extra_body = extra_body or {}
        self._azure = azure  # when {"api_version": ...} is non-empty, use AsyncAzureOpenAI

    def _build_client(self):
        import openai

        if self._azure:
            return openai.AsyncAzureOpenAI(
                api_key=self.credential.api_key.get_secret_value(),
                azure_endpoint=self.credential.base_url,
                api_version=self._azure.get("api_version", ""),
                http_client=self._http_client,
            )
        return openai.AsyncClient(
            api_key=self.credential.api_key.get_secret_value(),
            organization=self.credential.organization,
            base_url=self.credential.base_url,
            http_client=self._http_client,
        )

    async def _call_api(  # type: ignore[override]
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: ToolChoice | None = None,
        **generate_kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        # ⭐ Only difference from the parent class: inject the reused http_client (with read=600s timeout) / optional Azure client
        client = self._build_client()

        formatted_messages = await self.formatter.format(messages)

        kwargs: dict[str, Any] = {
            "model": model_name,
            "messages": formatted_messages,
            "stream": self.stream,
        }
        if self.parameters.max_tokens is not None:
            kwargs["max_tokens"] = self.parameters.max_tokens
        if self.parameters.temperature is not None:
            kwargs["temperature"] = self.parameters.temperature
        if self.parameters.top_p is not None:
            kwargs["top_p"] = self.parameters.top_p

        # ⭐ Inject the Qwen/minimax thinking-chain switch (via extra_body.chat_template_kwargs).
        # Azure OpenAI doesn't recognize this custom field; skip it to avoid 4xx.
        if self._extra_body and not self._azure:
            merged = dict(self._extra_body)
            merged.update(generate_kwargs.pop("extra_body", {}) or {})
            kwargs["extra_body"] = merged

        kwargs.update(generate_kwargs)

        fmt_tools, fmt_tool_choice = self._format_tools(tools, tool_choice)
        if fmt_tools:
            kwargs["tools"] = fmt_tools
            if not self.parameters.parallel_tool_calls:
                kwargs["parallel_tool_calls"] = False
        if fmt_tool_choice is not None:
            kwargs["tool_choice"] = fmt_tool_choice
        if self.stream:
            kwargs["stream_options"] = {"include_usage": True}

        start_datetime = datetime.now()
        response = await client.chat.completions.create(**kwargs)

        audio_cfg = kwargs.get("audio")
        audio_fmt = (
            audio_cfg.get("format", "wav") if isinstance(audio_cfg, dict) else "wav"
        )
        if self.stream:
            return self._parse_stream_response(start_datetime, response, audio_fmt)
        return self._parse_completion_response(start_datetime, response, audio_fmt)


def _make_http_client(timeout: int) -> httpx.AsyncClient:
    base_t = float(timeout) if timeout else 120.0
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=base_t,
            read=max(base_t, STREAM_READ_TIMEOUT_S),
            write=base_t,
            pool=base_t,
        )
    )


def _make_openai_compatible(
    spec,
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
    base_url: str,
    api_key: str,
    provider_extra: dict,
    disable_thinking: bool,
    reasoning_effort: Optional[str],
    stream: bool,
    context_size: int,
) -> OpenAICompatChatModel:
    azure: dict | None = None
    actual_model = model
    if spec.id == "azure_openai":
        azure = {"api_version": provider_extra.get("api_version", "")}
        actual_model = provider_extra.get("deployment") or model

    extra_body = {
        "chat_template_kwargs": _build_chat_template_kwargs(
            disable_thinking=disable_thinking,
            reasoning_effort=reasoning_effort,
        )
    }
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
        http_client=_make_http_client(timeout),
        context_size=context_size,
        extra_body=extra_body,
        azure=azure,
    )


def make_chat_model(
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
    base_url: str,
    api_key: str,
    provider: str = "openai_compatible",
    provider_extra: Optional[dict] = None,
    disable_thinking: bool = False,
    reasoning_effort: Optional[str] = None,
    stream: bool = False,
    context_size: Optional[int] = None,
) -> ChatModelBase:
    """Construct a ChatModel dispatched by provider (AgentScope 2.0).

    When the provider is unknown or empty, falls back to openai_compatible (backward
    compatible with existing data).

    ``context_size`` (the model's real context window; AS2 compaction trigger threshold =
    trigger_ratio × context_size) has **no default fallback**:
    - Not passed (None) → resolve the real context_length from the Config admin model
      configuration by model name; if unconfigured a ``ValueError`` is raised, forcing the
      configuration to be completed;
    - Explicitly passed a positive number → used directly. Restricted to two caller kinds:
      connectivity tests / tool-type LLMs (which never enter the agent compaction loop, so the
      value participates in no computation) and the placeholder dummy model.
    """
    provider_extra = provider_extra or {}
    spec = get_spec(provider)

    if not context_size or context_size <= 0:
        from core.llm.context_manager import resolve_model_context_window

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
    )


def _resolve_or_dummy(role_key: str):
    """Resolve config from DB, return None if not available."""
    try:
        from core.services.model_config import ModelConfigService
        return ModelConfigService.get_instance().resolve(role_key)
    except Exception as exc:
        logger.warning("ModelConfigService unavailable for role '%s': %s", role_key, exc)
        return None


def get_default_model(
    cfg: ModelConfig | None = None,
    disable_thinking: bool = False,
    reasoning_effort: Optional[str] = None,
    stream: bool = False,
) -> ChatModelBase:
    cfg = cfg or ModelConfig()
    resolved = _resolve_or_dummy("main_agent")
    if reasoning_effort is not None:
        supports = bool((resolved.extra if resolved else {}).get("supports_reasoning_effort"))
        if not supports:
            reasoning_effort = None
    if resolved:
        return make_chat_model(
            model=resolved.model_name,
            temperature=resolved.temperature,
            max_tokens=resolved.max_tokens,
            timeout=resolved.timeout,
            base_url=resolved.base_url,
            api_key=resolved.api_key,
            provider=resolved.provider,
            provider_extra=resolved.provider_extra,
            disable_thinking=disable_thinking,
            reasoning_effort=reasoning_effort,
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
