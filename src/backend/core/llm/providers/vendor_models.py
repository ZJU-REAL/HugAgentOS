"""ChatModel implementations + constructors for non-OpenAI-compat vendors.

- Native vendors (Anthropic / Gemini / DashScope / Ollama): thin subclasses that attach the
  L3 fallback mixin + the corresponding formatter; the underlying client is constructed by the
  native AgentScope class inside _call_api (no http_client injection point is exposed, so the
  timeout falls back to each SDK's default; the Anthropic SDK default timeout=600s satisfies
  long tool_call generation).
- litellm vendors (Bedrock, etc.): subclass OpenAIChatModel and only override _call_api to call
  litellm.acompletion instead, reusing OpenAIChatModel's OpenAI-format parser (litellm output is
  already OpenAI format). litellm is a lazy import; when not installed, only the litellm engine
  path raises a clear error without affecting other functionality.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, AsyncGenerator, Optional

import httpx
from agentscope.credential import (
    AnthropicCredential,
    DashScopeCredential,
    GeminiCredential,
    OllamaCredential,
    OpenAICredential,
)
from agentscope.formatter import (
    AnthropicChatFormatter,
    DashScopeChatFormatter,
    GeminiChatFormatter,
    OllamaChatFormatter,
    OpenAIChatFormatter,
)
from agentscope.message import Msg
from agentscope.model import (
    AnthropicChatModel,
    ChatResponse,
    DashScopeChatModel,
    GeminiChatModel,
    OllamaChatModel,
    OpenAIChatModel,
)
from agentscope.tool._types import ToolChoice

from .registry import ProviderSpec
from ._fallback import StructuredFallbackMixin

logger = logging.getLogger(__name__)


# ── Native vendor thin subclasses ─────────────────────────────────────────────
class NativeAnthropicChatModel(StructuredFallbackMixin, AnthropicChatModel):
    pass


class NativeGeminiChatModel(StructuredFallbackMixin, GeminiChatModel):
    pass


class NativeDashScopeChatModel(StructuredFallbackMixin, DashScopeChatModel):
    pass


class NativeOllamaChatModel(StructuredFallbackMixin, OllamaChatModel):
    pass


def build_native_model(
    spec: ProviderSpec,
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    base_url: str,
    api_key: str,
    context_size: int,
    stream: bool,
):
    """Construct the corresponding native vendor model per spec.native_class."""
    # context_size is guaranteed positive by make_chat_model (real window or caller-supplied
    # explicit value); always passed explicitly, no longer relying on the 128000 default that
    # the AS2 native class carries.
    ctx = {"context_size": context_size}

    if spec.native_class == "AnthropicChatModel":
        cred_kw: dict[str, Any] = {"api_key": api_key or "DUMMY"}
        if base_url:
            cred_kw["base_url"] = base_url
        return NativeAnthropicChatModel(
            credential=AnthropicCredential(**cred_kw),
            model=model or "claude-3-5-sonnet-latest",
            parameters=AnthropicChatModel.Parameters(max_tokens=max_tokens),
            stream=stream,
            formatter=AnthropicChatFormatter(),
            **ctx,
        )

    if spec.native_class == "GeminiChatModel":
        return NativeGeminiChatModel(
            credential=GeminiCredential(api_key=api_key or "DUMMY"),
            model=model or "gemini-1.5-pro",
            parameters=GeminiChatModel.Parameters(
                max_tokens=max_tokens, temperature=temperature
            ),
            stream=stream,
            formatter=GeminiChatFormatter(),
            **ctx,
        )

    if spec.native_class == "DashScopeChatModel":
        cred_kw = {"api_key": api_key or "DUMMY"}
        if base_url:
            cred_kw["base_url"] = base_url
        return NativeDashScopeChatModel(
            credential=DashScopeCredential(**cred_kw),
            model=model or "qwen-max",
            parameters=DashScopeChatModel.Parameters(
                max_tokens=max_tokens, temperature=temperature
            ),
            stream=stream,
            formatter=DashScopeChatFormatter(),
            **ctx,
        )

    if spec.native_class == "OllamaChatModel":
        return NativeOllamaChatModel(
            credential=OllamaCredential(host=base_url or "http://localhost:11434"),
            model=model or "llama3",
            parameters=OllamaChatModel.Parameters(
                max_tokens=max_tokens, temperature=temperature
            ),
            stream=stream,
            formatter=OllamaChatFormatter(),
            **ctx,
        )

    raise ValueError(f"未支持的原生厂商类：{spec.native_class}")


# ── litellm adapter ───────────────────────────────────────────────────────────
class LiteLLMChatModel(StructuredFallbackMixin, OpenAIChatModel):
    """Call any vendor via litellm; reuse OpenAIChatModel's OpenAI-format parser.

    litellm normalizes all vendor responses into OpenAI format (including streaming chunks), so
    they can be fed directly to the parent's ``_parse_stream_response`` /
    ``_parse_completion_response``.
    """

    def __init__(
        self,
        *,
        litellm_model: str,
        litellm_kwargs: dict,
        model: str,
        parameters: "OpenAIChatModel.Parameters",
        stream: bool,
        timeout: float,
        # required, no default (previously silently swallowed 128000; see the same comment in
        # chat_models.OpenAICompatChatModel)
        context_size: int,
    ) -> None:
        super().__init__(
            credential=OpenAICredential(api_key="litellm", base_url="https://litellm.invalid"),
            model=model or "litellm-model",
            parameters=parameters,
            stream=stream,
            max_retries=0,
            context_size=context_size,
            formatter=OpenAIChatFormatter(),
        )
        self._litellm_model = litellm_model
        self._litellm_kwargs = litellm_kwargs or {}
        self._timeout = timeout

    async def _call_api(  # type: ignore[override]
        self,
        model_name: str,
        messages: list[Msg],
        tools: list[dict] | None = None,
        tool_choice: ToolChoice | None = None,
        **generate_kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        try:
            import litellm
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "该 provider 需要 litellm（请在 requirements 增加 litellm 后重建镜像）"
            ) from exc

        formatted_messages = await self.formatter.format(messages)

        kwargs: dict[str, Any] = {
            "model": self._litellm_model,
            "messages": formatted_messages,
            "stream": self.stream,
            "timeout": self._timeout,
            **self._litellm_kwargs,
        }
        if self.parameters.max_tokens is not None:
            kwargs["max_tokens"] = self.parameters.max_tokens
        if self.parameters.temperature is not None:
            kwargs["temperature"] = self.parameters.temperature

        fmt_tools, fmt_tool_choice = self._format_tools(tools, tool_choice)
        if fmt_tools:
            kwargs["tools"] = fmt_tools
        if fmt_tool_choice is not None:
            kwargs["tool_choice"] = fmt_tool_choice
        if self.stream:
            kwargs["stream_options"] = {"include_usage": True}
        kwargs.update(generate_kwargs)

        start_datetime = datetime.now()
        response = await litellm.acompletion(**kwargs)

        if self.stream:
            return self._parse_stream_response(start_datetime, response, "wav")
        return self._parse_completion_response(start_datetime, response, "wav")


def build_litellm_model(
    spec: ProviderSpec,
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
    provider_extra: dict,
    context_size: int,
    stream: bool,
) -> LiteLLMChatModel:
    litellm_kwargs: dict[str, Any] = {}
    if spec.id == "bedrock":
        if provider_extra.get("aws_region"):
            litellm_kwargs["aws_region_name"] = provider_extra["aws_region"]
        if provider_extra.get("aws_access_key_id"):
            litellm_kwargs["aws_access_key_id"] = provider_extra["aws_access_key_id"]
        if provider_extra.get("aws_secret_access_key"):
            litellm_kwargs["aws_secret_access_key"] = provider_extra["aws_secret_access_key"]

    return LiteLLMChatModel(
        litellm_model=f"{spec.litellm_prefix}{model}",
        litellm_kwargs=litellm_kwargs,
        model=model,
        parameters=OpenAIChatModel.Parameters(
            temperature=temperature, max_tokens=max_tokens
        ),
        stream=stream,
        timeout=max(float(timeout or 120), 600.0),
        context_size=context_size,
    )
