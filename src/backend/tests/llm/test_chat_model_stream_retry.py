"""流式模型请求在首个事件之前失败时的有界重试：只重一次、只对瞬时类错误、首块之后不重。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest
from core.llm import chat_models


class _FakeModel:
    provider_id = "prov-1"

    def __init__(self, behaviours):
        # behaviours: list of ("raise", exc) | ("items", [...]) consumed per response object
        self._behaviours = list(behaviours)
        self.parsed = []

    async def _parse_stream_response(self, start_datetime, response, audio_fmt):
        kind, payload = self._behaviours.pop(0)
        self.parsed.append(response)
        if kind == "raise":
            raise payload
        for item in payload:
            yield item


def _client(second_response="resp-2"):
    create = AsyncMock(return_value=second_response)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), create


async def _collect(model, client, kwargs=None):
    return [
        item
        async for item in chat_models._stream_with_bounded_retry(
            model,
            client=client,
            kwargs=kwargs or {"messages": []},
            model_name="m",
            start_datetime=datetime.now(),
            response="resp-1",
            audio_fmt="wav",
            request_started=0.0,
        )
    ]


@pytest.fixture(autouse=True)
def _quiet_usage(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "core.llm.model_usage.record_provider_failure",
        AsyncMock(side_effect=lambda *a, **k: calls.append(("failure", k.get("metadata")))),
    )
    monkeypatch.setattr(
        "core.llm.model_usage.note_provider_retry_started",
        lambda *a, **k: calls.append(("retry_started", None)),
    )
    return calls


def _stream_error():
    # What the OpenAI client raises for an error event inside the SSE stream.
    return openai.APIError("upstream content blocked", httpx.Request("POST", "http://x"), body=None)


def test_retries_once_when_stream_fails_before_first_event(_quiet_usage):
    model = _FakeModel([("raise", _stream_error()), ("items", ["a", "b"])])
    client, create = _client()
    assert asyncio.run(_collect(model, client)) == ["a", "b"]
    create.assert_awaited_once()
    assert model.parsed == ["resp-1", "resp-2"]
    assert [c[0] for c in _quiet_usage] == ["failure", "retry_started"]


def test_second_failure_propagates_without_a_third_attempt(_quiet_usage):
    model = _FakeModel([("raise", _stream_error()), ("raise", _stream_error())])
    client, create = _client()
    with pytest.raises(openai.APIError):
        asyncio.run(_collect(model, client))
    create.assert_awaited_once()
    assert _quiet_usage[-1] == ("failure", {"fallback": "stream_start_retry_failed"})


@pytest.mark.parametrize(
    "exc",
    [
        openai.AuthenticationError("no", response=httpx.Response(401, request=httpx.Request("POST", "http://x")), body=None),
        openai.BadRequestError("bad", response=httpx.Response(400, request=httpx.Request("POST", "http://x")), body=None),
        RuntimeError("not an API error"),
    ],
)
def test_request_errors_are_not_retried(exc, _quiet_usage):
    model = _FakeModel([("raise", exc)])
    client, create = _client()
    with pytest.raises(type(exc)):
        asyncio.run(_collect(model, client))
    create.assert_not_awaited()
    assert _quiet_usage == []


def test_failure_after_first_chunk_is_not_retried(_quiet_usage):
    async def gen_then_fail(*_a, **_k):
        yield "a"
        raise _stream_error()

    model = SimpleNamespace(provider_id="p", _parse_stream_response=gen_then_fail)
    client, create = _client()
    with pytest.raises(openai.APIError):
        asyncio.run(_collect(model, client))
    create.assert_not_awaited()


def test_transient_classification():
    req = httpx.Request("POST", "http://x")
    assert chat_models._is_retryable_stream_start_error(openai.APIConnectionError(request=req))
    assert chat_models._is_retryable_stream_start_error(openai.APITimeoutError(request=req))
    assert chat_models._is_retryable_stream_start_error(
        openai.InternalServerError("x", response=httpx.Response(503, request=req), body=None)
    )
    assert chat_models._is_retryable_stream_start_error(
        openai.RateLimitError("x", response=httpx.Response(429, request=req), body=None)
    )
    assert not chat_models._is_retryable_stream_start_error(
        openai.NotFoundError("x", response=httpx.Response(404, request=req), body=None)
    )
