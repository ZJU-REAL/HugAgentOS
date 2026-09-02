"""Regression tests for model-provider connectivity probes."""

import pytest
from api.routes.v1 import models


@pytest.mark.asyncio
async def test_openai_chat_probe_matches_streaming_runtime(monkeypatch):
    captured = {}

    async def fake_http_ping(url, headers, payload, timeout=15):
        captured.update(
            {
                "url": url,
                "headers": headers,
                "payload": payload,
                "timeout": timeout,
            }
        )
        return {"success": True, "latency_ms": 1, "error": None}

    monkeypatch.setattr(models, "_http_ping", fake_http_ping)

    result = await models._ping_openai_compat(
        "chat",
        "http://model.test/api/v1/",
        "test-key",
        "test-model",
    )

    assert result["success"] is True
    assert captured["url"] == "http://model.test/api/v1/chat/completions"
    assert captured["payload"] == {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
        "stream": True,
    }
    assert captured["headers"]["Authorization"] == "Bearer test-key"


@pytest.mark.asyncio
async def test_non_v1_base_url_is_used_as_entered(monkeypatch):
    """Vendors served under /v4 (Zhipu, z.ai) must not get '/v1' spliced in."""
    urls = []

    async def fake_http_ping(url, headers, payload, timeout=15):
        urls.append(url)
        return {"success": True, "latency_ms": 1, "error": None}

    monkeypatch.setattr(models, "_http_ping", fake_http_ping)

    result = await models._test_connection(
        "openai_compatible",
        "chat",
        "https://api.z.ai/api/paas/v4",
        "test-key",
        "glm-4.6",
    )

    assert result["success"] is True
    assert urls == ["https://api.z.ai/api/paas/v4/chat/completions"]


def test_normalize_base_url_keeps_the_path():
    assert models._normalize_base_url("  https://api.z.ai/api/paas/v4/ ") == (
        "https://api.z.ai/api/paas/v4"
    )
    assert models._normalize_base_url("https://api.example.com") == "https://api.example.com"
