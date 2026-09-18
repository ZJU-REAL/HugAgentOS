"""一次性文本补全：追问、标题、分类、Wiki 抽取这些辅助调用的共同出口。

这些路径原先各自用裸 http 客户端硬拼 ``{base_url}/chat/completions``。端点走 Responses
协议时它们每次都 404——桌面本机端每答完一轮就白打一次模型网关，追问和标题静默失效。

端点说哪种协议，是配置时探测出来、存在 ``extra_config.api_protocol`` 里的事实（见
``core/llm/providers/protocol_probe``）。这里照主对话同一份事实选路径、组请求体、取回
文本；不按厂商名猜协议，也不在 404 之后回退到另一条线——协议是已知事实，回退只会把配置
错误掩盖成偶发失败。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

import httpx

from core.llm._distill_shared import strip_think_blocks
from core.llm.providers.protocol_probe import PROTOCOL_RESPONSES

_TEXT_ITEM = "output_text"


@dataclass(frozen=True)
class Endpoint:
    """调用一个模型端点所需的全部信息。"""

    base_url: str
    api_key: str
    model_name: str
    provider: str = "openai_compatible"
    api_protocol: Optional[str] = None

    @classmethod
    def from_resolved(cls, cfg: Any) -> "Endpoint":
        """由 ``ModelConfigService.resolve()`` 的结果构造。"""
        return cls(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model_name=cfg.model_name,
            provider=getattr(cfg, "provider", "") or "openai_compatible",
            api_protocol=(getattr(cfg, "extra", None) or {}).get("api_protocol"),
        )

    @property
    def speaks_responses(self) -> bool:
        from core.llm.chat_models import wants_responses
        from core.llm.providers.registry import get_spec

        return wants_responses(get_spec(self.provider), self.api_protocol)


@dataclass(frozen=True)
class Completion:
    text: str
    usage: dict[str, int]


class SingleTurnError(RuntimeError):
    """端点返回了非 200。"""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"{status} {body[:200]}")
        self.status = status
        self.body = body


def turns(prompt: str, system: Optional[str] = None) -> list[dict[str, str]]:
    """一问（可选带 system）的消息列表。"""
    items: list[dict[str, str]] = []
    if system:
        items.append({"role": "system", "content": system})
    items.append({"role": "user", "content": prompt})
    return items


def build_request(
    endpoint: Endpoint,
    messages: Sequence[Mapping[str, Any]],
    *,
    temperature: float,
    max_tokens: Optional[int] = None,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """把一段对话变成 ``(url, headers, json_body)``，路径按端点的协议选。

    ``messages`` 用 OpenAI chat 的 ``{role, content}`` 形状——调用方原本就这么组，
    Responses 线所需的输入项由这里转换，调用方不必知道自己在跟哪条线说话。

    凭据统一走桌面模型凭据模块：本机执行面的模型行存的是账号绑定的网关引用而不是密钥，
    非引用的普通密钥原样加 Bearer，所以云端和桌面共用这一条路径。
    """
    from core.services.desktop_model_credentials import prepare_request_headers

    base = endpoint.base_url.rstrip("/")
    headers = prepare_request_headers(endpoint.api_key, endpoint.base_url)

    if endpoint.speaks_responses:
        body: dict[str, Any] = {
            "model": endpoint.model_name,
            "input": [_responses_item(m) for m in messages],
            "stream": False,
            "store": False,
            "temperature": temperature,
        }
        if max_tokens is not None:
            body["max_output_tokens"] = max_tokens
        return f"{base}/{PROTOCOL_RESPONSES}", headers, body

    body = {
        "model": endpoint.model_name,
        "messages": [dict(m) for m in messages],
        "stream": False,
        "temperature": temperature,
        # 辅助调用只要结论，思考纯属浪费 token 和时延；这是兼容端点关闭思考的开关。
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    return f"{base}/chat/completions", headers, body


def stream_delta(endpoint: Endpoint, chunk: Mapping[str, Any]) -> str:
    """从一个 SSE 事件里取出正文增量。

    两条线的事件形状不一样，按端点说的协议取对应那种；取不到就是这个事件不带正文
    （思考、用量、生命周期事件），返回空串。
    """
    if endpoint.speaks_responses:
        if chunk.get("type") == "response.output_text.delta":
            delta = chunk.get("delta")
            if not isinstance(delta, str):
                raise ValueError("invalid content delta")
            return delta
        return ""
    parts: list[str] = []
    for choice in chunk.get("choices") or []:
        text = (choice.get("delta") or {}).get("content")
        if text is not None:
            if not isinstance(text, str):
                raise ValueError("invalid content delta")
            parts.append(text)
    return "".join(parts)


def stream_complete(endpoint: Endpoint, chunk: Mapping[str, Any]) -> bool:
    """这个事件是不是「答案已经完整」。

    收不到完结信号就说明流是断的——调用方据此拒绝返回半截答案。Chat 线用 ``[DONE]``
    哨兵（不是 JSON 事件，在 SSE 解析层判），Responses 线用 ``response.completed``。
    """
    return endpoint.speaks_responses and chunk.get("type") == "response.completed"


def _responses_item(message: Mapping[str, Any]) -> dict[str, Any]:
    """Responses 线的输入项；生产在用的 vLLM 严格校验，必须是完整的 content 块。"""
    role = str(message.get("role") or "user")
    # 助手说过的话在 Responses 线是模型的输出，块类型与输入侧不同。
    block = _TEXT_ITEM if role == "assistant" else "input_text"
    return {
        "role": role,
        "content": [{"type": block, "text": str(message.get("content") or "")}],
    }


def parse_response(endpoint: Endpoint, payload: dict[str, Any]) -> Completion:
    """把两条线各自的响应体归一成纯文本 + 统一命名的用量。"""
    if endpoint.speaks_responses:
        text = _responses_text(payload)
    else:
        choices = payload.get("choices") or [{}]
        text = str((choices[0].get("message") or {}).get("content") or "")
    return Completion(text=strip_think_blocks(text).strip(), usage=_usage(payload.get("usage")))


def _responses_text(payload: dict[str, Any]) -> str:
    """只取 ``output_text``：思考在 Responses 线是独立的 reasoning 项，天然不混进来。"""
    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") == _TEXT_ITEM:
                parts.append(str(block.get("text") or ""))
    return "".join(parts)


def _usage(raw: Any) -> dict[str, int]:
    """两条线的用量字段名不同，归一成记账侧的命名。"""
    if not isinstance(raw, dict):
        return {}
    cached = (raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}).get(
        "cached_tokens"
    )
    return {
        "prompt_tokens": int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0),
        "completion_tokens": int(raw.get("completion_tokens") or raw.get("output_tokens") or 0),
        "cache_read_tokens": int(raw.get("cache_read_tokens") or cached or 0),
        "cache_write_tokens": int(raw.get("cache_write_tokens") or 0),
    }


async def complete(
    endpoint: Endpoint,
    prompt: str,
    *,
    system: Optional[str] = None,
    temperature: float,
    max_tokens: Optional[int] = None,
    timeout: float = 30.0,
) -> Completion:
    url, headers, body = build_request(
        endpoint, turns(prompt, system), temperature=temperature, max_tokens=max_tokens
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=body)
    if resp.status_code != 200:
        raise SingleTurnError(resp.status_code, resp.text)
    return parse_response(endpoint, resp.json())
