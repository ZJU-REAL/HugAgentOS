"""Effort discovery must distinguish declarations, rejection and unknown transport errors."""

import httpx
import pytest
from core.llm.providers.reasoning_probe import discover_reasoning_efforts


@pytest.mark.asyncio
async def test_probe_reads_explicit_supported_levels_from_error(monkeypatch):
    original = httpx.AsyncClient

    def reply(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "vision"}]})
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Invalid reasoning effort for vision: medium, should be int within [1,100] or ['low', 'high', 'xhigh', 'max']"
                }
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(reply), **kwargs),
    )
    result = await discover_reasoning_efforts(
        base_url="http://model.test/v1",
        api_key="secret",
        model_name="vision",
        provider="openai_compatible",
        api_protocol="responses",
    )
    assert [level["key"] for level in result["levels"]] == ["low", "high", "xhigh", "max"]
    assert result["default"] == "high"
    assert result["numeric_range"] == [1, 100]
    assert result["source"] == "validation_error"


@pytest.mark.asyncio
async def test_probe_does_not_guess_when_authentication_fails(monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(401, json={"error": "unauthorized"})
            ),
            **kwargs,
        ),
    )
    result = await discover_reasoning_efforts(
        base_url="http://model.test/v1",
        api_key="secret",
        model_name="vision",
        provider="openai_compatible",
        api_protocol="responses",
    )
    assert result["levels"] == []
    assert result["source"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("numeric_only", [True, False])
async def test_probe_preserves_range_and_marks_partial_results(monkeypatch, numeric_only):
    original = httpx.AsyncClient

    def reply(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        if numeric_only:
            return httpx.Response(
                400, json={"error": {"message": "reasoning effort must be int within [1,100]"}}
            )
        if request.read().decode().find("medium") >= 0:
            return httpx.Response(200, text='data: {"type":"response.completed"}')
        return httpx.Response(429, text="rate limited")

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(reply), **kwargs),
    )
    result = await discover_reasoning_efforts(
        base_url="http://model.test/v1",
        api_key="secret",
        model_name="vision",
        provider="openai_compatible",
        api_protocol="responses",
    )
    if numeric_only:
        assert result["numeric_range"] == [1, 100]
        assert result["levels"] == []
    else:
        assert result["complete"] is False
        assert result["levels"] == [{"key": "medium", "value": "medium"}]
