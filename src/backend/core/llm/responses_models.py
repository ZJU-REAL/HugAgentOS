"""OpenAI **Responses** 线路：OpenAI 兼容上游的默认协议，与 OpenAICompatChatModel 对应。

``/chat/completions`` 没有放思考的地方，思考模型又要求把上一步的思考原样带回（否则
调过工具的那轮 400），chat 线为此靠 ``ReasoningEchoChatFormatter`` 塞
``reasoning_content``。Responses 把思考做成独立的 ``reasoning`` 条目，排在它产生的
``function_call`` 前即可。走哪条由 ``extra_config.api_protocol`` 决定，是
``providers/protocol_probe`` 配置期探测出来的事实，不靠厂商名猜。

AgentScope 自带的 ``OpenAIResponseModel`` 回发 reasoning 条目时只带 id，``content``
恒为空、也不带 ``encrypted_content`` —— 而这两样正是 codex 做思考回传所依赖的
（``codex-rs/core/src/client.rs``、``codex-rs/protocol/src/models.rs``）。本模块补上：
留下上游给出的整条条目，下一轮原样发回，不解释也不重拼。上游没多给东西时自然空转。
"""

from __future__ import annotations

import logging
from datetime import datetime
from time import monotonic as _monotonic
from functools import lru_cache
from typing import Any, AsyncGenerator, Optional, get_args

import httpx
from agentscope.credential import OpenAICredential
from agentscope.formatter import OpenAIResponseFormatter
from agentscope.message import Msg, ThinkingBlock
from agentscope.model import ChatResponse, OpenAIResponseModel
from agentscope.tool._types import ToolChoice

from core.llm.providers._fallback import StructuredFallbackMixin
from core.llm.providers._image_tokens import ImageTokenCountingMixin
from core.llm.reasoning_replay import ReasoningReplayMixin
from core.llm.tool_call_identity import ToolCallIdentityMixin
from core.llm.tool_result_media import ResponsesToolMediaMixin

logger = logging.getLogger(__name__)

WIRE_PROTOCOL = "openai_responses"

# 挂在 ThinkingBlock 上（它声明了 extra="allow"），随块一起进历史。
REASONING_ITEMS_ATTR = "reasoning_items"

# 只有这两个终态事件会带上完整的 response 对象（含 output 数组）。
_TERMINAL_EVENTS = ("response.completed", "response.incomplete")

# Responses 有两条思考通道：``reasoning_summary_text`` 是经过删减的摘要（OpenAI 返回
# 的那一条），``reasoning_text`` 是模型的思考原文（自建推理服务返回的那一条）。
# AgentScope 的解析器只认前者，后者整条被丢掉——实测自建 deepseekv4-flash-vision 一轮
# 发 144 帧 reasoning_text.delta，界面上就是一个空的思考块。两条通道对界面是同一件东西，
# 所以在事件入口把后者翻成前者，流式逐字、累积、进历史全部照旧由父类处理。
_REASONING_TEXT_DELTA = "response.reasoning_text.delta"
_REASONING_SUMMARY_DELTA = "response.reasoning_summary_text.delta"

# 对齐 codex：让上游把思考加密串交还给我们自己保管，服务端不留会话状态。
_REASONING_INCLUDE = ["reasoning.encrypted_content"]


@lru_cache(maxsize=1)
def _wire_reasoning_efforts() -> frozenset[str]:
    """Which effort values ``reasoning.effort`` actually accepts, read off the SDK type."""
    values: set[str] = set()

    def walk(annotation: Any) -> None:
        for arg in get_args(annotation):
            if isinstance(arg, str):
                values.add(arg)
            else:
                walk(arg)

    walk(OpenAIResponseModel.Parameters.model_fields["reasoning_effort"].annotation)
    return frozenset(values)


# 产品档位名与 Responses 的词汇表大多同名，只有「超高」两边叫法不同：产品叫 max，
# API 叫 xhigh。翻译它而不是丢掉它——在这条线上思考开关就是 reasoning.effort（实测
# DeepSeek 的 Responses 端点只认它，不看 chat_template_kwargs），丢掉等于把用户选的
# 「思考·超高」悄悄变成不思考。
_PRODUCT_TO_WIRE_EFFORT = {"max": "xhigh"}


def native_reasoning_effort(effort: Optional[str]) -> Optional[str]:
    """把产品的思考档位翻译成 ``reasoning.effort`` 认得的词。

    词汇表从 SDK 的类型声明里读，不写死：翻译后的值若不在其中（换了 SDK 版本、或传进来
    一个新档位），宁可不发这个字段，也不要把上游一定会 400 的值送出去——实测两个自建
    端点对 ``effort: "max"`` 直接返回 400。
    """
    if not effort:
        return None
    wire = _PRODUCT_TO_WIRE_EFFORT.get(effort, effort)
    return wire if wire in _wire_reasoning_efforts() else None


def _as_dict(item: Any) -> dict[str, Any]:
    """把 SDK 返回的条目对象转成可原样回发的 dict。"""
    if isinstance(item, dict):
        return dict(item)
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except TypeError:
            return dump()
    return {}


class _ReasoningTextDelta:
    """把思考原文增量伪装成解析器认得的摘要增量。

    父类只读 ``type`` 和 ``delta`` 两个字段（``response`` 用于取 response_id，这类事件
    本来就没有），所以一个同形的小对象足够，不必构造 SDK 的类型。
    """

    __slots__ = ("type", "delta", "response", "summary_index")

    def __init__(self, delta: str) -> None:
        self.type = _REASONING_SUMMARY_DELTA
        self.delta = delta
        self.response = None
        self.summary_index = 0


class _ReasoningItemCapture:
    """透传上游 SSE，旁路留下完整 reasoning 条目，并接上思考原文通道。

    旁路而非重写解析器：那 130 行逐事件拼装跟着 SDK 版本走，抄过来就得跟着维护。
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self.items: list[dict[str, Any]] = []

    def __aiter__(self):  # noqa: ANN204
        return self._iter()

    async def _iter(self):  # noqa: ANN202
        async for event in self._stream:
            kind = getattr(event, "type", None)
            if kind in _TERMINAL_EVENTS:
                self._collect(getattr(event, "response", None))
            elif kind == _REASONING_TEXT_DELTA:
                delta = getattr(event, "delta", None)
                if delta:
                    yield _ReasoningTextDelta(delta)
                continue
            yield event

    def _collect(self, response: Any) -> None:
        for item in getattr(response, "output", None) or []:
            kind = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
            if kind == "reasoning":
                data = _as_dict(item)
                if data:
                    self.items.append(data)


def _item_text(items: list[dict[str, Any]]) -> str:
    """条目里模型自己的思考正文（不含加密串——那是给上游回读的，不是给人看的）。"""
    parts: list[str] = []
    for item in items:
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("text"):
                parts.append(str(part["text"]))
        for part in item.get("summary") or []:
            if isinstance(part, dict) and part.get("text"):
                parts.append(str(part["text"]))
    return "\n".join(parts)


def _attach_reasoning_items(content: Any, items: list[dict[str, Any]]) -> None:
    """把完整条目挂到这一轮产出的 ThinkingBlock 上，顺带兜住没走增量通道的思考。

    有的上游只在终态条目里给思考正文，一帧增量都不发；那样界面上会是个空思考块。
    只在块本身没有正文时才回填，绝不覆盖已经逐字流出来的内容。
    """
    if not items:
        return
    for block in content or []:
        if isinstance(block, ThinkingBlock):
            setattr(block, REASONING_ITEMS_ATTR, [dict(item) for item in items])
            if not (block.thinking or "").strip():
                block.thinking = _item_text(items)
            return


def _restore_reasoning_items(
    msgs: list[Msg], items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """把 AgentScope 发的精简 reasoning 条目换回上游原本给的那一条。

    AgentScope 只保留了最后一个 reasoning 条目的 id，一轮里若有多条，它也只发一条
    占位。这里按 id 找回整组，原样铺开——顺序就是上游给的顺序，紧挨着它产生的
    ``function_call``，与 codex 的历史布局一致。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for msg in msgs:
        for block in msg.content or []:
            if not isinstance(block, ThinkingBlock):
                continue
            captured = getattr(block, REASONING_ITEMS_ATTR, None) or []
            if not captured:
                continue
            # AgentScope 回发的是最后一条的 id；按它建索引才对得上占位条目。
            anchor = str(captured[-1].get("id") or "")
            if anchor:
                groups[anchor] = captured

    if not groups:
        return items

    restored: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict) and item.get("type") == "reasoning":
            captured = groups.get(str(item.get("id") or ""))
            if captured:
                restored.extend(dict(one) for one in captured)
                continue
        restored.append(item)
    return restored


class ResponsesReplayFormatter(
    ReasoningReplayMixin, ResponsesToolMediaMixin, OpenAIResponseFormatter
):
    """Responses 线格式化：思考按上游原样回传，工具媒体绝不落盘。

    ``ReasoningReplayMixin`` 负责跨模型安全——只有同一 provider/model/protocol 产出的
    思考才会被当作原生思考回传，别的模型留下的会降级成一段带标注的普通文本，不会伪
    造成当前模型的推理。
    """

    def _format_response_data_block(self, block: Any, role: str = "user") -> Any:
        """给每个 ``input_image`` 补上 ``detail``——严格实现少了它直接 400。

        AgentScope 只产出 ``type`` + ``image_url``，而 OpenAI 的
        ``ResponseInputImageParam`` 把 ``detail`` 列为必填。官方端点和 DeepSeek 官方
        都不校验，所以缺字段在那两家看着完全正常；生产在用的 vLLM 严格校验，整轮请求
        直接 ``1 validation error ... detail Field required``。codex 的 ``view_image``
        也是每次都带。

        管这条线上的每张图，不只工具返回的：用户上传的图走的是同一个方法。
        ``setdefault`` 是为了不覆盖上游将来自己给出的值。
        """
        item = super()._format_response_data_block(block, role)  # type: ignore[misc]
        if isinstance(item, dict) and item.get("type") == "input_image":
            item.setdefault("detail", "auto")
        return item

    async def format(self, msgs: list[Msg]) -> list[dict[str, Any]]:
        items = await super().format(msgs)
        return _restore_reasoning_items(msgs, items)


class OpenAICompatResponsesModel(
    ToolCallIdentityMixin,
    ImageTokenCountingMixin,
    StructuredFallbackMixin,
    OpenAIResponseModel,
):
    """OpenAIResponseModel 子类：复用本项目的 http 客户端、失败记账与思考留存。

    与 ``OpenAICompatChatModel`` 一一对应，区别只在线路：那边打
    ``/chat/completions``，这边打 ``/responses``。
    """

    wire_protocol = WIRE_PROTOCOL

    # Responses 线上思考永远以独立的 reasoning 条目到达，不可能内嵌成
    # ``<think>`` 正文，所以这是协议事实而非逐模型配置：SSE 层据此在流开始就下发
    # structured_reasoning 标记，模型不思考的那几轮前端也不会把正文误当思考缓冲。
    structured_reasoning = True

    def __init__(
        self,
        *,
        credential: OpenAICredential,
        model: str,
        parameters: "OpenAIResponseModel.Parameters",
        stream: bool,
        http_client: httpx.AsyncClient,
        # 无默认值：AS2 用 context_size 算压缩触发阈值，静默继承上游默认值会让
        # 真 256k 的模型在半窗就开始压缩（与 OpenAICompatChatModel 同因）。
        context_size: int,
        provider_id: str = "openai_compatible",
        extra_body: dict | None = None,
        reasoning_effort: Optional[str] = None,
    ) -> None:
        super().__init__(
            credential=credential,
            model=model,
            parameters=parameters,
            stream=stream,
            # max_retries=0：重试只由 agent 层（ModelConfig.max_retries）负责，
            # 避免两层相乘。
            max_retries=0,
            context_size=context_size,
            formatter=ResponsesReplayFormatter(
                replay_provider=provider_id,
                replay_model=model,
                replay_protocol=WIRE_PROTOCOL,
            ),
        )
        self._http_client = http_client
        self.provider_id = provider_id
        self._extra_body = extra_body or {}
        self._reasoning_effort = native_reasoning_effort(reasoning_effort)
        self._context_rewrite_listener = None

    def set_context_rewrite_listener(self, listener) -> None:  # noqa: ANN001
        self._context_rewrite_listener = listener

    def _build_client(self):  # noqa: ANN202
        import openai

        # 每轮 ReAct 都新建 SDK 客户端会重复解析 base_url、重建鉴权头（一轮工具密集
        # 的对话有 15-20 次），所以按模型实例缓存一个薄封装。
        cached = getattr(self, "_openai_client", None)
        if cached is not None:
            return cached
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
        from core.llm.chat_models import (
            _dump_wire_payload,
            _slim_tool_schemas,
            _stream_with_bounded_retry,
        )

        client = self._build_client()
        formatted_input = await self.formatter.format(messages)

        kwargs: dict[str, Any] = {
            "model": model_name,
            "input": formatted_input,
            "stream": self.stream,
            # 思考归客户端保管：不在服务端留状态，加密串随响应发回来，我们自己存。
            "store": False,
            "include": list(_REASONING_INCLUDE),
        }
        if self.parameters.max_tokens is not None:
            kwargs["max_output_tokens"] = self.parameters.max_tokens
        if self.parameters.temperature is not None:
            kwargs["temperature"] = self.parameters.temperature
        if self._reasoning_effort:
            kwargs["reasoning"] = {"effort": self._reasoning_effort}
        if self._extra_body:
            merged = dict(self._extra_body)
            merged.update(generate_kwargs.pop("extra_body", {}) or {})
            kwargs["extra_body"] = merged

        kwargs.update(generate_kwargs)

        fmt_tools, fmt_tool_choice = self._format_tools(tools, tool_choice)
        if fmt_tools:
            kwargs["tools"] = _slim_tool_schemas(fmt_tools)
        if fmt_tool_choice is not None:
            kwargs["tool_choice"] = fmt_tool_choice

        start_datetime = datetime.now()
        # 记账用单调时钟：墙钟会被系统校时拖出负数/跳变的时延。
        usage_started = _monotonic()
        _dump_wire_payload(kwargs)

        async def _issue():  # noqa: ANN202
            return await client.responses.create(**kwargs)

        try:
            response = await _issue()
        except Exception as exc:
            from core.llm.model_usage import record_provider_failure

            logger.warning("Responses request failed: %s: %s", type(exc).__name__, exc)
            await record_provider_failure(
                self,
                model_name,
                exc,
                started=usage_started,
                provider=self.provider_id,
            )
            raise

        if self.stream:
            return _stream_with_bounded_retry(
                self,
                reissue=_issue,
                parse=lambda started, raw: self._parse_stream_response(started, raw),
                model_name=model_name,
                start_datetime=start_datetime,
                response=response,
                request_started=usage_started,
            )
        return self._parse_completion_response(start_datetime, response)

    async def _publish_context_rewrite(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Observe and optionally canonicalize a provider-level retry request.

        Mirrors ``OpenAICompatChatModel``: the orchestration layer installs a listener
        on every request but only a retry consumes it, so the normal path never pays
        for a context re-assembly.
        """
        import inspect

        from core.llm.context_adapter import PROVIDER_CONTEXT_META_KEY

        listener = self._context_rewrite_listener
        if listener is not None:
            rendered = listener(messages)
            if inspect.isawaitable(rendered):
                rendered = await rendered
            if rendered is not None:
                messages = list(rendered)
        cleaned = []
        for message in messages:
            row = dict(message)
            row.pop(PROVIDER_CONTEXT_META_KEY, None)
            cleaned.append(row)
        return cleaned

    async def _parse_stream_response(  # type: ignore[override]
        self,
        start_datetime: datetime,
        response: Any,
    ) -> AsyncGenerator[ChatResponse, None]:
        capture = _ReasoningItemCapture(response)
        async for chunk in super()._parse_stream_response(start_datetime, capture):
            if getattr(chunk, "is_last", False):
                _attach_reasoning_items(chunk.content, capture.items)
            yield chunk

    def _parse_completion_response(  # type: ignore[override]
        self,
        start_datetime: datetime,
        response: Any,
    ) -> ChatResponse:
        parsed = super()._parse_completion_response(start_datetime, response)
        items = [
            _as_dict(item)
            for item in getattr(response, "output", None) or []
            if getattr(item, "type", None) == "reasoning"
        ]
        _attach_reasoning_items(parsed.content, [one for one in items if one])
        return parsed
