"""Discover whether an endpoint can actually be driven over the Responses protocol.

Which of ``/responses`` and ``/chat/completions`` an endpoint serves is a fact about
that endpoint, not something to infer from its vendor name: one preset covers both
self-hosted vLLM and thin relays that will never implement Responses. So it is probed
once at configuration time and persisted into ``extra_config.api_protocol``, the same
treatment ``context_probe`` gives the context window.

**Routing the path is not the same as speaking the protocol.** An endpoint can answer
``/responses`` and still ignore ``function_call_output`` — the model then never sees a
tool result and calls the same tool forever. Measured on a self-hosted Qwen3.6 vLLM:
route present, tool results dropped. That is worse than not supporting Responses at
all, so the probe has to establish usability, not reachability:

1. **Route** — ``POST {base_url}/responses`` without ``input``. A server that serves the
   path rejects the body; one that does not answers 404. Status only, no error-text
   matching, and no inference runs, so this stage cannot bill tokens.
2. **Tool result round trip** — replay a one-call history whose result carries a token
   the model could not otherwise know, and see whether the answer uses it or re-issues
   the same call. This one does run inference; it is bounded to a few hundred tokens and
   happens only at configuration time, never on the request path.

Anything inconclusive (down, rate-limited, bad credential) records nothing, because a
guess would silently pin a capable model to the older protocol — or, worse, pin a
broken one to the newer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# The two values ``extra_config.api_protocol`` may hold.
PROTOCOL_RESPONSES = "responses"
PROTOCOL_CHAT = "chat_completions"

PROTOCOL_LABELS = {
    PROTOCOL_RESPONSES: "Responses API",
    PROTOCOL_CHAT: "Chat Completions",
}

# Statuses that mean "this server does not route /responses". Everything else —
# including 400/422 (the body was rejected, so the route exists) and 5xx (the
# handler ran and blew up on the missing field) — proves the route is there.
_NO_ROUTE_STATUSES = frozenset({404, 405, 501})

# Bumped whenever the probe starts checking something it did not check before, so the
# startup backfill knows which recorded answers predate the stricter test and re-runs
# them. Version 1 only proved the route existed.
PROBE_VERSION = 2

# The tool-result round trip. The token is arbitrary and unguessable so that "the model
# repeated it" cannot happen by chance, and the tool takes no arguments so a re-issued
# call is unambiguous rather than a plausible refinement.
_ROUND_TRIP_TOKEN = "KQ7413"
_ROUND_TRIP_TOOL = "probe_get_token"
_ROUND_TRIP_MAX_OUTPUT = 256


@dataclass
class ProtocolProbeResult:
    """Outcome of one protocol discovery attempt.

    ``protocol`` is empty when nothing conclusive was seen; ``notes`` carries the
    explanation for the admin UI.
    """

    protocol: str = ""
    status_code: int = 0
    detail: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.protocol)

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_protocol": self.protocol,
            "api_protocol_label": PROTOCOL_LABELS.get(self.protocol, ""),
            "status_code": self.status_code,
            "detail": self.detail,
            "notes": list(self.notes),
        }


def _endpoint(base_url: str) -> str:
    return f"{(base_url or '').rstrip('/')}/responses"


async def detect_api_protocol(
    *,
    base_url: str,
    api_key: str,
    model_name: str,
    timeout: int = 15,
) -> ProtocolProbeResult:
    """Ask *base_url* whether it routes ``/responses``.

    Never raises: discovery is a convenience on the configuration path, never a
    precondition for saving a provider. An unreachable endpoint yields an empty
    result with the reason in ``notes``.
    """
    result = ProtocolProbeResult()
    if not (base_url or "").strip():
        result.notes.append("base_url 为空，无法探测协议")
        return result

    url = _endpoint(base_url)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Deliberately incomplete body: ``input`` is required, so a server that routes
    # this path fails validation before any inference happens.
    payload = {"model": model_name or ""}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        result.notes.append(f"探测 {url} 失败：{type(exc).__name__}: {exc}")
        return result

    result.status_code = response.status_code
    result.detail = (response.text or "")[:200]

    if response.status_code in _NO_ROUTE_STATUSES:
        result.protocol = PROTOCOL_CHAT
        result.notes.append(
            f"{url} 返回 {response.status_code}，该上游没有 Responses 端点，使用 Chat Completions"
        )
        return result

    if response.status_code in (401, 403):
        result.notes.append(
            f"{url} 返回 {response.status_code}（凭据被拒），无法判断协议，保持原设置"
        )
        return result

    result.notes.append(f"{url} 返回 {response.status_code}（不是 404，说明该路由存在）")
    return await _check_tool_result_round_trip(
        result, url=url, headers=headers, model_name=model_name, timeout=timeout
    )


async def _check_tool_result_round_trip(
    result: ProtocolProbeResult,
    *,
    url: str,
    headers: dict[str, str],
    model_name: str,
    timeout: int,
) -> ProtocolProbeResult:
    """Does the endpoint actually feed ``function_call_output`` back to the model?

    An endpoint that serves the route but drops tool results is unusable for an agent:
    every tool call is answered by the same tool call again. Pinning such an endpoint to
    chat completions is not a workaround — chat completions is the protocol it really
    speaks.
    """
    history = [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        f"调用 {_ROUND_TRIP_TOOL} 取得口令后，直接把口令原样复述给我，"
                        "不要再调用任何工具。"
                    ),
                }
            ],
        },
        {
            "type": "function_call",
            "id": "fc_probe",
            "call_id": "call_probe",
            "name": _ROUND_TRIP_TOOL,
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "call_id": "call_probe",
            "output": f"口令是 {_ROUND_TRIP_TOKEN}",
        },
    ]
    payload = {
        "model": model_name or "",
        "input": history,
        "tools": [
            {
                "type": "function",
                "name": _ROUND_TRIP_TOOL,
                "description": "取得本次任务口令",
                "parameters": {"type": "object", "properties": {}, "required": []},
            }
        ],
        "max_output_tokens": _ROUND_TRIP_MAX_OUTPUT,
        "store": False,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, headers=headers, json=payload)
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        result.notes.append(f"工具结果回灌探测失败：{type(exc).__name__}: {exc}；无法判断，保持原设置")
        return result

    if response.status_code != 200:
        result.notes.append(
            f"工具结果回灌探测返回 {response.status_code}，无法判断，保持原设置"
        )
        return result

    try:
        output = response.json().get("output") or []
    except ValueError:
        result.notes.append("工具结果回灌探测的响应不是 JSON，无法判断，保持原设置")
        return result

    spoken = " ".join(
        part.get("text") or ""
        for item in output
        if isinstance(item, dict)
        for part in (item.get("content") or [])
        if isinstance(part, dict)
    )
    called_again = any(
        isinstance(item, dict) and item.get("type") == "function_call" for item in output
    )

    if _ROUND_TRIP_TOKEN in spoken:
        result.protocol = PROTOCOL_RESPONSES
        result.notes.append("工具结果被模型读到了，Responses 可用")
        return result

    if called_again:
        result.protocol = PROTOCOL_CHAT
        result.notes.append(
            "该上游的 Responses 端点不消费 function_call_output（模型又调了一次同一个工具），"
            "智能体循环会卡死，改用 Chat Completions"
        )
        return result

    result.notes.append("工具结果回灌探测既没复述口令也没重复调用，无法判断，保持原设置")
    return result
