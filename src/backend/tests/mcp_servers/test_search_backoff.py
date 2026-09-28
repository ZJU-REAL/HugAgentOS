"""Bounded async retry behavior, exercised through the search service."""

import asyncio

import httpx
import pytest
from mcp_servers.internet_search_mcp import impl
from mcp_servers.internet_search_mcp.models import SearchRequest


@pytest.mark.parametrize(
    "statuses, expected_calls, expected_status",
    [
        ([429, 429, 200], 3, "complete"),
        ([429, 429, 429], 3, "error"),
        ([400], 1, "error"),
    ],
)
def test_retry_statuses(monkeypatch, statuses, expected_calls, expected_status):
    monkeypatch.setattr(
        impl,
        "get_runtime_value",
        lambda name: {"INTERNET_SEARCH_ENGINE": "langsearch", "LANGSEARCH_API_KEY": "test"}.get(
            name
        ),
    )

    async def no_sleep(delay):
        pass

    monkeypatch.setattr(impl.asyncio, "sleep", no_sleep)
    calls = []

    async def run():
        async def handler(request):
            calls.append(request)
            return httpx.Response(
                statuses[len(calls) - 1], json={"code": 200, "data": {"webPages": {"value": []}}}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await impl.SearchService(client).search(SearchRequest(query="test"))
        assert result["status"] == expected_status
        assert len(calls) == expected_calls

    asyncio.run(run())


def test_retry_after_is_not_shortened(monkeypatch):
    monkeypatch.setattr(
        impl,
        "get_runtime_value",
        lambda name: {"INTERNET_SEARCH_ENGINE": "tavily", "TAVILY_API_KEY": "test"}.get(name),
    )
    waited = []

    async def no_sleep(delay):
        waited.append(delay)

    monkeypatch.setattr(impl.asyncio, "sleep", no_sleep)

    async def run():
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(
                429 if calls == 1 else 200, headers={"Retry-After": "7"}, json={"results": []}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await impl.SearchService(client).search(SearchRequest(query="test"))
        assert result["status"] == "complete"
        assert waited[0] >= 7

    asyncio.run(run())


def test_retry_after_outside_budget_reports_failure(monkeypatch):
    monkeypatch.setattr(
        impl,
        "get_runtime_value",
        lambda name: {"INTERNET_SEARCH_ENGINE": "tavily", "TAVILY_API_KEY": "test"}.get(name),
    )

    async def run():
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(429, headers={"Retry-After": "300"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await impl.SearchService(client).search(SearchRequest(query="test"))
        assert calls == 1
        assert result["status"] == "error"
        assert result["query_statuses"][0]["error_code"] == "http_429"

    asyncio.run(run())
