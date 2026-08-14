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
import os
from datetime import datetime
from time import perf_counter as _perf_counter
from typing import Any, AsyncGenerator, Optional

import httpx
from agentscope.credential import OpenAICredential
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import Msg
from agentscope.model import ChatModelBase, ChatResponse, OpenAIChatModel
from agentscope.tool._types import ToolChoice
from core.llm.providers._fallback import (  # noqa: F401
    L3_SYNTHETIC_METADATA,
    StructuredFallbackMixin,
)
from core.llm.providers.registry import get_spec, split_provider_extra
from core.llm.providers.vendor_models import build_litellm_model, build_native_model
from prompts.prompt_config import ModelConfig

logger = logging.getLogger(__name__)


# See the 1.x comment: read timeout raised separately (default 600s), leaving ample
# time for long tool_call args generation. Env-tunable: a hung LLM gateway holds a
# run for this long per attempt before the client errors and retries — ops can lower
# it (e.g. 240) when the upstream endpoint is known to be flaky, so retries and the
# final error surface well within the run's lifetime.
STREAM_READ_TIMEOUT_S: float = float(os.getenv("LLM_STREAM_READ_TIMEOUT_S", "600"))

_MULTIMODAL_CONTENT_TYPES = frozenset(
    {
        "audio",
        "image",
        "image_url",
        "input_audio",
        "input_image",
    }
)


def _is_multimodal_unsupported_error(exc: Exception) -> bool:
    """Whether an OpenAI-compatible endpoint explicitly rejected media input."""
    message = str(exc).lower()
    if "not a multimodal model" in message:
        return True
    mentions_media = any(word in message for word in ("image", "audio", "multimodal"))
    rejects_media = any(
        phrase in message
        for phrase in (
            "does not support",
            "doesn't support",
            "not supported",
            "unsupported",
        )
    )
    return mentions_media and rejects_media


def _without_multimodal_content(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Copy formatted messages while replacing unsupported media blocks with text."""
    sanitized: list[dict[str, Any]] = []
    removed = 0
    fallback_text = (
        "<system-reminder>工具返回了图片或音频，但当前模型不支持直接读取该媒体。"
        "请依据工具结果中已有的文字、元数据和图注继续完成回答，不要因此中止。</system-reminder>"
    )

    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            sanitized.append(message)
            continue

        kept: list[Any] = []
        removed_from_message = 0
        for block in content:
            block_type = str(block.get("type", "")).lower() if isinstance(block, dict) else ""
            if block_type in _MULTIMODAL_CONTENT_TYPES:
                removed += 1
                removed_from_message += 1
            else:
                kept.append(block)

        if not removed_from_message:
            sanitized.append(message)
            continue

        # AgentScope promotes multimodal tool outputs into a synthetic
        # ``system-reminder`` user message. Its remaining identifier prose is
        # meaningless once the media block is removed, so replace the whole
        # reminder. For ordinary user messages, retain their accompanying text
        # and append the same explicit degradation notice.
        if message.get("name") == "system-reminder":
            kept = [{"type": "text", "text": fallback_text}]
        else:
            kept.append({"type": "text", "text": fallback_text})
        sanitized.append({**message, "content": kept})

    return sanitized, removed


# Off unless someone creates the marker file inside the container:
#   docker exec <backend> touch /tmp/jx-wire-dump
# Then the next model call writes its exact request body to /tmp/jx-wire.json.
# A marker file rather than an env var so it can be flipped on a running
# container without a restart — the point is to capture what a live
# conversation actually sends when a first token comes back slow.
_WIRE_DUMP_MARKER = "/tmp/jx-wire-dump"
_WIRE_DUMP_PATH = "/tmp/jx-wire.json"


def _dump_wire_payload(kwargs: dict) -> None:
    """Write the outgoing request body to disk when the marker file exists."""
    try:
        if not os.path.exists(_WIRE_DUMP_MARKER):
            return
        import json as _json

        with open(_WIRE_DUMP_PATH, "w") as handle:
            _json.dump(
                {
                    "messages": kwargs.get("messages"),
                    "tools": kwargs.get("tools"),
                    "extra_body": kwargs.get("extra_body"),
                    "params": {
                        k: v
                        for k, v in kwargs.items()
                        if k not in ("messages", "tools", "extra_body")
                    },
                },
                handle,
                ensure_ascii=False,
            )
    except Exception:  # pragma: no cover - diagnostics must never break a run
        logger.debug("[wire] payload dump failed", exc_info=True)


def _collapse_nullable(node: Any) -> Any:
    """Rewrite ``anyOf: [T, {"type": "null"}]`` in place as plain ``T``.

    Pydantic renders every ``Optional[str] = None`` parameter that way, which
    costs roughly 45 characters per optional field while saying nothing the
    sibling ``default: null`` doesn't already say. Across a 64-tool surface
    that is real prefill on every request and every ReAct round.
    """
    if isinstance(node, dict):
        variants = node.get("anyOf")
        if (
            isinstance(variants, list)
            and len(variants) == 2
            and {"type": "null"} in variants
        ):
            other = next((v for v in variants if v != {"type": "null"}), None)
            if isinstance(other, dict):
                node.pop("anyOf")
                for key, value in other.items():
                    node.setdefault(key, value)
        for value in list(node.values()):
            _collapse_nullable(value)
    elif isinstance(node, list):
        for value in node:
            _collapse_nullable(value)
    return node


def _slim_tool_schemas(tools: list[dict]) -> list[dict]:
    """Strip prefill-only waste from tool schemas without changing their meaning.

    Two transforms, both purely syntactic:
      * nullable unions collapsed (see :func:`_collapse_nullable`);
      * descriptions run through ``inspect.cleandoc``, which drops the uniform
        indentation every Python docstring inherits from its ``def`` body while
        preserving relative indentation inside examples.

    Measured on this deployment's 64-tool surface: 28,049 → 26,888 prompt
    tokens (-4.1%), paid back on every round of every run. The model sees the
    same names, types, constraints and prose.
    """
    import copy
    import inspect

    slimmed = _collapse_nullable(copy.deepcopy(tools))
    for tool in slimmed:
        fn = tool.get("function")
        if not isinstance(fn, dict):
            continue
        if isinstance(fn.get("description"), str):
            fn["description"] = inspect.cleandoc(fn["description"])
        props = (fn.get("parameters") or {}).get("properties")
        if isinstance(props, dict):
            for prop in props.values():
                if isinstance(prop, dict) and isinstance(prop.get("description"), str):
                    prop["description"] = inspect.cleandoc(prop["description"])
    return slimmed


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
        structured_reasoning: bool = False,
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
        # The SSE layer uses this to distinguish structured reasoning_content from
        # legacy models that embed reasoning in content as <think>...</think>.
        self.structured_reasoning = structured_reasoning

    def _build_client(self):
        import openai

        # Built once per model instance: the SDK client is a thin wrapper over
        # the shared httpx client, but it is re-created on every ReAct round
        # otherwise, which re-parses the base_url and re-builds the auth headers
        # for each of the 15-20 rounds a tool-heavy run makes.
        cached = getattr(self, "_openai_client", None)
        if cached is not None:
            return cached

        if self._azure:
            client = openai.AsyncAzureOpenAI(
                api_key=self.credential.api_key.get_secret_value(),
                azure_endpoint=self.credential.base_url,
                api_version=self._azure.get("api_version", ""),
                http_client=self._http_client,
            )
        else:
            client = openai.AsyncClient(
                api_key=self.credential.api_key.get_secret_value(),
                organization=self.credential.organization,
                base_url=self.credential.base_url,
                http_client=self._http_client,
            )
        self._openai_client = client
        return client

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
            kwargs["tools"] = _slim_tool_schemas(fmt_tools)
            if not self.parameters.parallel_tool_calls:
                kwargs["parallel_tool_calls"] = False
        if fmt_tool_choice is not None:
            kwargs["tool_choice"] = fmt_tool_choice
        if self.stream:
            kwargs["stream_options"] = {"include_usage": True}

        start_datetime = datetime.now()
        try:
            # Prefill size is the dominant TTFT term against a gateway without
            # prefix caching, and it is invisible from the outside — this line
            # is what lets a slow first token be attributed to the payload
            # (system prompt + tool schemas) rather than to the network.
            _t_req = _perf_counter()
            _dump_wire_payload(kwargs)
            if logger.isEnabledFor(logging.INFO):
                import json as _json

                logger.info(
                    "[wire] OUT model=%s msgs=%d msg_chars=%d tools=%d tool_chars=%d",
                    model_name,
                    len(kwargs.get("messages") or []),
                    len(_json.dumps(kwargs.get("messages") or [], ensure_ascii=False)),
                    len(kwargs.get("tools") or []),
                    len(_json.dumps(kwargs.get("tools") or [], ensure_ascii=False)),
                )
            response = await client.chat.completions.create(**kwargs)
            logger.info("[wire] response headers in %.0fms", (_perf_counter() - _t_req) * 1000)
        except Exception as exc:
            # MCP tools may return image DataBlocks alongside useful JSON text
            # (for example get_paper_figures). AgentScope promotes those blocks
            # into an OpenAI image_url message for the next ReAct round. A
            # text-only compatible endpoint rejects the whole request with a
            # 400, which used to terminate the run immediately after the tool
            # succeeded. Preserve multimodal input by default; only after an
            # endpoint explicitly rejects it, retry once with the media blocks
            # removed while retaining the tool's text/metadata/captions.
            if not _is_multimodal_unsupported_error(exc):
                raise
            fallback_messages, removed = _without_multimodal_content(formatted_messages)
            if not removed:
                raise
            logger.warning(
                "Model %s rejected multimodal input; retrying without %d media block(s)",
                model_name,
                removed,
            )
            kwargs["messages"] = fallback_messages
            response = await client.chat.completions.create(**kwargs)

        audio_cfg = kwargs.get("audio")
        audio_fmt = audio_cfg.get("format", "wav") if isinstance(audio_cfg, dict) else "wav"
        if self.stream:
            return self._parse_stream_response(start_datetime, response, audio_fmt)
        return self._parse_completion_response(start_datetime, response, audio_fmt)


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
_HTTP_CLIENTS: dict[tuple[int, float], httpx.AsyncClient] = {}

# Keep enough idle sockets for concurrent runs (main agent + subagents + memory
# extraction all talk to the same gateway) without letting an idle pool pin
# connections open forever against a gateway that culls them.
_POOL_LIMITS = httpx.Limits(
    max_connections=int(os.getenv("LLM_HTTP_MAX_CONNECTIONS", "100")),
    max_keepalive_connections=int(os.getenv("LLM_HTTP_MAX_KEEPALIVE", "40")),
    keepalive_expiry=float(os.getenv("LLM_HTTP_KEEPALIVE_EXPIRY_S", "300")),
)


def _make_http_client(timeout: int) -> httpx.AsyncClient:
    import asyncio

    base_t = float(timeout) if timeout else 120.0
    try:
        loop_key = id(asyncio.get_running_loop())
    except RuntimeError:
        # No running loop (sync construction in scripts/tests) — don't cache,
        # since we can't tell which loop will end up driving this client.
        loop_key = 0

    key = (loop_key, base_t)
    client = _HTTP_CLIENTS.get(key)
    if client is not None and not client.is_closed:
        return client

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=base_t,
            read=max(base_t, STREAM_READ_TIMEOUT_S),
            write=base_t,
            pool=base_t,
        ),
        limits=_POOL_LIMITS,
    )
    if loop_key:
        _HTTP_CLIENTS[key] = client
    return client


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

    extra_body: dict[str, Any] = {
        "chat_template_kwargs": _build_chat_template_kwargs(
            disable_thinking=disable_thinking,
            reasoning_effort=reasoning_effort,
        )
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
        http_client=_make_http_client(timeout),
        context_size=context_size,
        extra_body=extra_body,
        azure=azure,
        structured_reasoning=spec.structured_reasoning,
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
