"""OpenAI-compatible transport, schemas and reasoning formatter."""

from __future__ import annotations

import inspect
import logging
import os
from datetime import datetime
from time import monotonic as _monotonic
from time import perf_counter as _perf_counter
from typing import Any, AsyncGenerator

import httpx
from agentscope.credential import OpenAICredential
from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import (
    DataBlock,
    HintBlock,
    Msg,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from agentscope.model import ChatResponse, OpenAIChatModel
from agentscope.tool._types import ToolChoice
from core.llm.context_adapter import PROVIDER_CONTEXT_META_KEY
from core.llm.context_safety import check_request_admission
from core.llm.providers._image_tokens import ImageTokenCountingMixin
from core.llm.providers.stream_retry import _stream_with_bounded_retry
from core.llm.reasoning_replay import ReasoningReplayMixin
from core.llm.tool_call_identity import ToolCallIdentityMixin
from core.llm.tool_result_media import NoDiskToolMediaMixin

logger = logging.getLogger(__name__)


def _safe_exception_chain(exc: BaseException) -> list[dict[str, str]]:
    """Identify wrapped SDK failures without serializing messages or requests."""
    from core.capabilities.errors import CapabilityError

    known_codes = {
        "capability_error",
        "package_missing",
        "name_conflict",
        "view_unavailable",
        "integrity_failed",
        "install_conflict",
        "cloud_unavailable",
        "permission_denied",
        "dependency_missing",
    }
    chain: list[dict[str, str]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(chain) < 8 and id(current) not in seen:
        seen.add(id(current))
        kind = type(current)
        entry = {"type": f"{kind.__module__}.{kind.__qualname__}"}
        if isinstance(current, CapabilityError) and kind.code in known_codes:
            entry["code"] = kind.code
        chain.append(entry)
        current = current.__cause__ or current.__context__
    return chain


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
        if isinstance(variants, list) and len(variants) == 2 and {"type": "null"} in variants:
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

    Both OpenAI tool shapes are accepted: chat completions nests the schema under
    ``function``, Responses puts the same keys on the tool itself.
    """
    import copy
    import inspect

    slimmed = _collapse_nullable(copy.deepcopy(tools))
    for tool in slimmed:
        fn = tool.get("function")
        if not isinstance(fn, dict):
            fn = tool
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
    reasoning_effort: str | int | None,
) -> dict:
    """Build chat_template_kwargs (Qwen/minimax thinking-chain switch, via extra_body)."""
    if disable_thinking:
        return {"enable_thinking": False}
    if reasoning_effort is None:
        return {"enable_thinking": True}
    if reasoning_effort == "medium":
        return {"thinking": True}
    return {"thinking": True, "reasoning_effort": reasoning_effort}


def _reasoning_steps(msg: Msg) -> list[list[Any]]:
    """Split one Msg's blocks into the ReAct steps it accumulated.

    A whole ReAct loop lands in a single Msg: ``AgentScope`` extends the last assistant
    message in place on every step (``agent/_agent.py``), so one Msg can hold
    think → call → result → think → call → result → think → answer. The parent formatter
    already flushes a separate assistant row per step; splitting on the same boundaries is
    what lets each row carry the reasoning that produced *it*. Attaching the Msg's whole
    reasoning to every row instead would rewrite history — the second tool call would
    appear to have been reasoned with conclusions drawn after it — and would resend the
    same text once per step, diverging from what the IR budgeted for it once.

    The parent flushes when a tool result or hint arrives and something is pending, so a
    step ends exactly there. Thinking blocks never make a row pending on their own.
    """

    steps: list[list[Any]] = []
    current: list[Any] = []
    pending = False
    for block in msg.get_content_blocks():
        if isinstance(block, (ToolResultBlock, HintBlock)) and pending:
            steps.append(current)
            current = []
            pending = False
        current.append(block)
        if isinstance(block, (TextBlock, DataBlock, ToolCallBlock)):
            pending = True
    if current:
        steps.append(current)
    return steps or [[]]


class ReasoningEchoChatFormatter(ReasoningReplayMixin, NoDiskToolMediaMixin, OpenAIChatFormatter):
    """OpenAI wire format, except the model's own reasoning is handed back to it.

    ``OpenAIChatFormatter`` drops every ThinkingBlock, since the OpenAI API has no field
    for reasoning in conversation history. Thinking-mode endpoints such as DeepSeek V4
    invert that rule: an assistant message that arrives without the ``reasoning_content``
    it produced is refused with a 400, which in practice kills exactly the turns that
    called a tool — the only ones that send an assistant message back — so the symptom is
    an answer cut off the moment the tools finish.

    A step that did no thinking sends no field at all, matching deepseek-harness
    (``reasoning.length > 0 ? { reasoning_content } : {}``). Endpoints that never emit
    reasoning on this channel, OpenAI included, therefore see a byte-identical request.
    """

    async def format(self, msgs: list[Msg]) -> list[dict[str, Any]]:
        self.assert_list_of_msgs(msgs)
        msgs = self.prepare_replay(msgs)

        messages: list[dict[str, Any]] = []
        for msg in msgs:
            for step in _reasoning_steps(msg):
                rows = await super().format([msg.model_copy(update={"content": step})])
                reasoning = "\n".join(
                    block.thinking
                    for block in step
                    if isinstance(block, ThinkingBlock) and block.thinking
                )
                if reasoning:
                    for row in rows:
                        if row.get("role") == "assistant":
                            row["reasoning_content"] = reasoning
                messages.extend(rows)
        return messages


class OpenAICompatChatModel(ToolCallIdentityMixin, ImageTokenCountingMixin, OpenAIChatModel):
    """OpenAIChatModel subclass: injects a custom http_client + extra_body; optional Azure OpenAI client.

    Pinned to agentscope==2.0.0: the ``_call_api`` body is copied from the parent class (2.0.0);
    sync it when upgrading upstream.
    """

    # Recorded on every model step so replay knows which wire format produced it.
    wire_protocol = "openai_chat"

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
        provider_id: str = "openai_compatible",
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
            formatter=ReasoningEchoChatFormatter(
                replay_provider=provider_id,
                replay_model=model,
                replay_protocol="openai_chat",
            ),
        )
        self._http_client = http_client
        self.provider_id = provider_id
        self._extra_body = extra_body or {}
        self._azure = azure  # when {"api_version": ...} is non-empty, use AsyncAzureOpenAI
        # The SSE layer uses this to distinguish structured reasoning_content from
        # legacy models that embed reasoning in content as <think>...</think>.
        self.structured_reasoning = structured_reasoning
        self._context_rewrite_listener = None

    def set_context_rewrite_listener(self, listener) -> None:  # noqa: ANN001
        """Observe and optionally canonicalize a provider-level retry request."""
        self._context_rewrite_listener = listener

    async def _publish_context_rewrite(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        listener = self._context_rewrite_listener
        if listener is not None:
            rendered = listener(messages)
            if inspect.isawaitable(rendered):
                rendered = await rendered
            if rendered is not None:
                messages = list(rendered)
        # Harness metadata is never a provider API field, including when this
        # model is invoked outside ManifestBoundAgent.
        cleaned = []
        for message in messages:
            row = dict(message)
            row.pop(PROVIDER_CONTEXT_META_KEY, None)
            cleaned.append(row)
        return cleaned

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
        # ⭐ Only difference from the parent class: inject the reused http_client (with the configured timeout) / optional Azure client
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
            _usage_started = _monotonic()
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
            check_request_admission()
            response = await client.chat.completions.create(**kwargs)
            logger.info("[wire] response headers in %.0fms", (_perf_counter() - _t_req) * 1000)
        except Exception as exc:
            from core.llm.model_usage import record_provider_failure

            logger.warning("Model request failed: exception_chain=%s", _safe_exception_chain(exc))
            await record_provider_failure(
                self,
                model_name,
                exc,
                started=_usage_started,
                provider=self.provider_id,
            )
            raise

        audio_cfg = kwargs.get("audio")
        audio_fmt = audio_cfg.get("format", "wav") if isinstance(audio_cfg, dict) else "wav"
        if self.stream:
            return _stream_with_bounded_retry(
                self,
                reissue=lambda: client.chat.completions.create(**kwargs),
                parse=lambda started, raw: self._parse_stream_response(started, raw, audio_fmt),
                model_name=model_name,
                start_datetime=start_datetime,
                response=response,
                request_started=_usage_started,
            )
        return self._parse_completion_response(start_datetime, response, audio_fmt)
