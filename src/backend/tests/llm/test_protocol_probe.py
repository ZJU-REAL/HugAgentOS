"""协议探测：靠 HTTP 状态判定路由存在与否，探不出来就什么都不写。

判定必须只看状态码，不看错误文案——各家上游对「缺 input」的措辞、甚至状态码都不一样
（实测 vLLM 系 400、另一家 500），一旦改成匹配文案，换个上游就失灵。
"""

from __future__ import annotations

import httpx
import pytest

from core.llm.providers import protocol_probe
from core.llm.providers.protocol_probe import (
    PROTOCOL_CHAT,
    PROTOCOL_RESPONSES,
    detect_api_protocol,
)


# 第二阶段（工具结果回灌）默认让它"通过"，这样只想测路由判定的用例不必关心它。
_CONSUMED = {
    "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "口令是 KQ7413"}]}
    ]
}
# 路由在、但模型无视工具结果又调了一次——这正是必须判回 chat 的情形。
_IGNORED = {
    "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "我来查一下"}]},
        {"type": "function_call", "name": "probe_get_token", "arguments": "{}"},
    ]
}


def _stub(
    monkeypatch,
    *,
    status: int = 400,
    body: str = "",
    raises: Exception | None = None,
    round_trip: dict | None = None,
    round_trip_status: int = 200,
):
    """第一次 POST 是路由探测，之后的是工具结果回灌探测。"""
    captured: dict = {"calls": []}

    class _Client:
        def __init__(self, *a, **kw):
            captured["timeout"] = kw.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["calls"].append(json)
            captured.setdefault("url", url)
            captured.setdefault("headers", headers or {})
            captured.setdefault("json", json)
            if len(captured["calls"]) == 1:
                if raises is not None:
                    raise raises
                return httpx.Response(status, text=body)
            payload = _CONSUMED if round_trip is None else round_trip
            return httpx.Response(round_trip_status, json=payload)

    monkeypatch.setattr(protocol_probe.httpx, "AsyncClient", _Client)
    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 400, 422, 500])
async def test_any_non_404_status_means_the_route_exists(monkeypatch, status):
    _stub(monkeypatch, status=status)

    result = await detect_api_protocol(
        base_url="http://up.test/v1", api_key="k", model_name="m"
    )

    assert result.protocol == PROTOCOL_RESPONSES
    assert result.found


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 405, 501])
async def test_missing_route_pins_the_endpoint_to_chat_completions(monkeypatch, status):
    _stub(monkeypatch, status=status, body='{"error":"Not Found"}')

    result = await detect_api_protocol(
        base_url="http://relay.test/api/v1", api_key="k", model_name="m"
    )

    assert result.protocol == PROTOCOL_CHAT


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_credentials_decide_nothing(monkeypatch, status):
    """凭据不对时上游没告诉我们任何协议信息，写下任何一边都是猜。"""
    _stub(monkeypatch, status=status)

    result = await detect_api_protocol(
        base_url="http://up.test/v1", api_key="bad", model_name="m"
    )

    assert not result.found
    assert result.protocol == ""


@pytest.mark.asyncio
async def test_unreachable_endpoint_never_raises(monkeypatch):
    _stub(monkeypatch, raises=httpx.ConnectError("boom"))

    result = await detect_api_protocol(
        base_url="http://down.test/v1", api_key="k", model_name="m"
    )

    assert not result.found


@pytest.mark.asyncio
async def test_empty_base_url_is_reported_not_probed(monkeypatch):
    captured = _stub(monkeypatch)

    result = await detect_api_protocol(base_url="", api_key="k", model_name="m")

    assert not result.found
    assert "url" not in captured


@pytest.mark.asyncio
async def test_probe_cannot_bill_tokens(monkeypatch):
    """故意不带 input：上游在参数校验阶段就拒绝，绝不进推理。"""
    captured = _stub(monkeypatch)

    await detect_api_protocol(
        base_url="http://up.test/v1/", api_key="k", model_name="my-model"
    )

    assert captured["url"] == "http://up.test/v1/responses"
    assert captured["json"] == {"model": "my-model"}
    assert "input" not in captured["json"]
    assert captured["headers"]["Authorization"] == "Bearer k"


@pytest.mark.asyncio
async def test_anonymous_endpoint_sends_no_auth_header(monkeypatch):
    captured = _stub(monkeypatch)

    await detect_api_protocol(base_url="http://up.test/v1", api_key="", model_name="m")

    assert "Authorization" not in captured["headers"]


@pytest.mark.asyncio
async def test_a_route_that_drops_tool_results_is_not_a_usable_responses_endpoint(monkeypatch):
    """路由在、却不消费 function_call_output —— 智能体循环会卡死，判回 chat。

    实测自建 Qwen3.6 vLLM 就是这样：收到工具结果当没看见，把同一个工具再调一遍。
    """
    _stub(monkeypatch, status=400, round_trip=_IGNORED)

    result = await detect_api_protocol(
        base_url="http://qwen.test/v1", api_key="k", model_name="m"
    )

    assert result.protocol == PROTOCOL_CHAT


@pytest.mark.asyncio
async def test_a_route_that_feeds_tool_results_back_is_usable(monkeypatch):
    _stub(monkeypatch, status=400, round_trip=_CONSUMED)

    result = await detect_api_protocol(
        base_url="http://up.test/v1", api_key="k", model_name="m"
    )

    assert result.protocol == PROTOCOL_RESPONSES


@pytest.mark.asyncio
async def test_the_round_trip_replays_a_real_tool_result(monkeypatch):
    captured = _stub(monkeypatch, status=400)

    await detect_api_protocol(base_url="http://up.test/v1", api_key="k", model_name="m")

    round_trip = captured["calls"][1]
    kinds = [item.get("type") or item.get("role") for item in round_trip["input"]]
    assert "function_call" in kinds and "function_call_output" in kinds
    assert round_trip["max_output_tokens"] <= 512


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"round_trip_status": 500},
        {"round_trip": {"output": [{"type": "message", "content": [{"type": "output_text", "text": "嗯"}]}]}},
    ],
)
async def test_an_inconclusive_round_trip_decides_nothing(monkeypatch, kwargs):
    """第二阶段没结论时不写库——错判成 chat 会白白丢掉思考回传。"""
    _stub(monkeypatch, status=400, **kwargs)

    result = await detect_api_protocol(
        base_url="http://up.test/v1", api_key="k", model_name="m"
    )

    assert not result.found


@pytest.mark.asyncio
async def test_a_missing_route_never_pays_for_the_round_trip(monkeypatch):
    """没有路由就不必再发第二个请求——那一个是要花推理钱的。"""
    captured = _stub(monkeypatch, status=404)

    result = await detect_api_protocol(
        base_url="http://relay.test/v1", api_key="k", model_name="m"
    )

    assert result.protocol == PROTOCOL_CHAT
    assert len(captured["calls"]) == 1
