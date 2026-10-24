"""Search contract tests: real async HTTP boundary, no external API calls."""

import asyncio

import httpx
import pytest
from extensions.mcp_servers.internet_search_mcp.impl import SearchService
from extensions.mcp_servers.internet_search_mcp.models import SearchRequest


def test_parallel_round_robin_deduplicates_and_preserves_dates(monkeypatch):
    monkeypatch.setattr(
        "extensions.mcp_servers.internet_search_mcp.impl.get_runtime_value",
        lambda name: {"INTERNET_SEARCH_ENGINE": "langsearch", "LANGSEARCH_API_KEY": "test"}.get(
            name
        ),
    )

    async def run():
        entered = 0
        barrier = asyncio.Event()

        async def handler(request):
            nonlocal entered
            import json

            query = json.loads(request.content)["query"]
            entered += 1
            if entered == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), 1)
            values = [
                {
                    "name": query,
                    "url": "https://example.org/shared",
                    "summary": "English source",
                    "datePublished": "2026-09-20",
                },
                {"name": query, "url": f"https://example.org/{query}", "summary": query},
            ]
            return httpx.Response(200, json={"code": 200, "data": {"webPages": {"value": values}}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await SearchService(client).search(
                SearchRequest(queries=["A", "B"], max_results=8)
            )
        assert result["status"] == "complete"
        assert [r["url"] for r in result["results"]] == [
            "https://example.org/shared",
            "https://example.org/A",
            "https://example.org/B",
        ]
        assert result["results"][0]["matched_queries"] == [0, 1]
        assert result["results"][0]["published_date"] == "2026-09-20"

    asyncio.run(run())


def test_removed_filter_and_malformed_topic_are_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SearchRequest(query="test", cn_only=True)
    with pytest.raises(ValidationError):
        SearchRequest(query="test", topic={"query": "test"})


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "A", "queries": ["B"]},
        {"queries": []},
        {"queries": ["a", "b", "c", "d", "e"]},
        {"query": "A", "max_results": 9},
        {"query": "A", "max_results": True},
        {"query": "A", "include_raw_content": "false"},
    ],
)
def test_invalid_inputs(arguments):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SearchRequest(**arguments)


def test_query_normalization():
    assert SearchRequest(queries=[" A ", "", "A", "B"]).query_list() == ["A", "B"]


@pytest.mark.parametrize("engine", ["baidu", "deepseek", "langsearch", "tavily"])
def test_provider_contract_and_country(monkeypatch, engine):
    settings = {
        "INTERNET_SEARCH_ENGINE": engine,
        "BAIDU_API_KEY": "test",
        "DEEPSEEK_SEARCH_API_KEY": "test",
        "LANGSEARCH_API_KEY": "test",
        "TAVILY_API_KEY": "test",
    }
    monkeypatch.setattr("extensions.mcp_servers.internet_search_mcp.impl.get_runtime_value", settings.get)

    async def run():
        import json

        calls = []

        async def handler(request):
            calls.append(json.loads(request.content))
            source = {
                "title": "English official docs",
                "url": "https://example.org",
                "content": "Only English",
                "published_date": "2026-09-20",
            }
            if engine == "baidu":
                payload = {"references": [{**source, "date": "2026-09-20"}]}
            elif engine == "deepseek":
                payload = {
                    "results": [
                        {
                            "title": source["title"],
                            "url": source["url"],
                            "summary": source["content"],
                            "date": "2026-09-20T16:00:00Z",
                            "rerank_score": 2.0,
                        }
                    ]
                }
            elif engine == "langsearch":
                payload = {
                    "code": 200,
                    "data": {
                        "webPages": {
                            "value": [
                                {
                                    "name": source["title"],
                                    "url": source["url"],
                                    "summary": source["content"],
                                    "datePublished": "2026-09-20",
                                }
                            ]
                        }
                    },
                }
            else:
                payload = {"results": [source]}
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = SearchService(client)
            result = await service.search(SearchRequest(query="docs"))
            assert len(result["results"]) == 1
            assert "country" not in calls[-1]
            if engine == "baidu":
                assert result["results"][0]["date_raw"] == "2026-09-20"
            if engine == "tavily":
                settings["INTERNET_SEARCH_COUNTRY"] = "china"
                await service.search(SearchRequest(query="docs"))
                assert calls[-1]["country"] == "china"
                await service.search(SearchRequest(query="docs", topic="news"))
                assert "country" not in calls[-1]

    asyncio.run(run())


def test_deepseek_request_and_result_mapping(monkeypatch):
    import json
    from datetime import date

    monkeypatch.setattr(
        "extensions.mcp_servers.internet_search_mcp.impl.get_runtime_value",
        lambda name: {"INTERNET_SEARCH_ENGINE": "deepseek", "DEEPSEEK_SEARCH_API_KEY": "sk"}.get(
            name
        ),
    )

    async def run():
        calls = []

        async def handler(request):
            calls.append((request, json.loads(request.content)))
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "rerank_score": 2.4,
                            "title": "Relevant",
                            "url": "https://example.org/a",
                            "snippet": "short",
                            "summary": "long model summary",
                            "content": "full page",
                            "date": "2026-10-01T16:00:00Z",
                            "authority": 3,
                        },
                        {
                            "rerank_score": 0.1,
                            "title": "Noise",
                            "url": "https://example.org/noise",
                            "snippet": "noise",
                        },
                        {"title": "Unscored", "url": "https://example.org/b", "snippet": "only"},
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = SearchService(client)
            result = await service.search(
                SearchRequest(query="docs", max_results=3, include_raw_content=True)
            )
            request, payload = calls[-1]
            assert str(request.url) == "https://api.deepseek.com/v1/tools/search"
            assert request.headers["Authorization"] == "Bearer sk"
            assert payload == {"query": "docs", "count": 6, "need_content": True}
            assert [r["url"] for r in result["results"]] == [
                "https://example.org/a",
                "https://example.org/b",
            ]
            first = result["results"][0]
            assert first["content"] == "long model summary"
            assert first["raw_content"] == "full page"
            assert first["published_date"] == "2026-10-01T16:00:00Z"
            assert result["results"][1]["content"] == "only"

            await service.search(SearchRequest(query="fed rate", topic="news"))
            news_query = calls[-1][1]["query"]
            assert news_query.startswith("fed rate after:")
            assert date.fromisoformat(news_query.rsplit(":", 1)[1]) < date.today()
            assert calls[-1][1]["need_content"] is False

            sent = len(calls)
            too_long = await service.search(SearchRequest(query="x" * 129))
            assert len(calls) == sent
            assert too_long["status"] == "error"
            assert too_long["query_statuses"][0]["error_code"] == "query_too_long"

    asyncio.run(run())


def test_deepseek_rejects_malformed_results(monkeypatch):
    monkeypatch.setattr(
        "extensions.mcp_servers.internet_search_mcp.impl.get_runtime_value",
        lambda name: {"INTERNET_SEARCH_ENGINE": "deepseek", "DEEPSEEK_SEARCH_API_KEY": "sk"}.get(
            name
        ),
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"results": ["not a row"]})
            )
        ) as client:
            result = await SearchService(client).search(SearchRequest(query="a"))
        assert result["query_statuses"][0]["error_code"] == "invalid_response"

    asyncio.run(run())


def test_partial_failure_still_gets_citations(monkeypatch):
    import json

    from core.llm.citations.allocator import AnchorAllocator
    from core.llm.citations.anchor import annotate_tool_result

    monkeypatch.setattr(
        "extensions.mcp_servers.internet_search_mcp.impl.get_runtime_value",
        lambda name: {"INTERNET_SEARCH_ENGINE": "tavily", "TAVILY_API_KEY": "test"}.get(name),
    )

    async def run():
        async def handler(request):
            if json.loads(request.content)["query"] == "bad":
                return httpx.Response(401, json={"error": "secret provider details"})
            return httpx.Response(
                200,
                json={
                    "results": [{"title": "docs", "url": "https://example.org", "content": "proof"}]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await SearchService(client).search(SearchRequest(queries=["good", "bad"]))
        assert result["status"] == "partial"
        assert result["query_statuses"][1]["error_code"] == "http_401"
        assert "secret provider" not in json.dumps(result)
        text, citations = annotate_tool_result(
            "internet_search", "call1", json.dumps({"result": result}), AnchorAllocator()
        )
        assert len(citations) == 1
        assert json.loads(text)["result"]["results"][0]["cite_id"]

    asyncio.run(run())


def test_deadline_keeps_completed_queries_and_cleans_up(monkeypatch):
    import json

    monkeypatch.setattr(
        "extensions.mcp_servers.internet_search_mcp.impl.get_runtime_value",
        lambda name: {"INTERNET_SEARCH_ENGINE": "tavily", "TAVILY_API_KEY": "test"}.get(name),
    )

    async def run():
        exited = asyncio.Event()

        async def handler(request):
            if json.loads(request.content)["query"] == "slow":
                try:
                    await asyncio.Event().wait()
                finally:
                    exited.set()
            return httpx.Response(200, json={"results": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await SearchService(client, batch_timeout=0.05).search(
                SearchRequest(queries=["fast", "slow"])
            )
        assert exited.is_set()
        assert result["status"] == "partial"
        assert result["query_statuses"][1]["error_code"] == "timeout"

    asyncio.run(run())


def test_caller_cancellation_releases_tasks_and_capacity(monkeypatch):
    monkeypatch.setattr(
        "extensions.mcp_servers.internet_search_mcp.impl.get_runtime_value",
        lambda name: {"INTERNET_SEARCH_ENGINE": "tavily", "TAVILY_API_KEY": "test"}.get(name),
    )

    async def run():
        started = asyncio.Event()
        exited = asyncio.Event()

        async def handler(request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            service = SearchService(client)
            task = asyncio.create_task(service.search(SearchRequest(query="A")))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert exited.is_set()
            # A second request can enter the service after cancellation.
            second = asyncio.create_task(service.search(SearchRequest(query="B")))
            await asyncio.sleep(0)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second

    asyncio.run(run())


def test_merge_limits_and_url_identity():
    from extensions.mcp_servers.internet_search_mcp.merge import merge_sources

    def source(url):
        return {"url": url, "title": "doc", "content": "x" * 3000, "raw_content": "y" * 9000}

    result = merge_sources(
        [
            {
                "results": [
                    source("https://EXAMPLE.org:443/a"),
                    source("https://example.org/a?x=1"),
                    source("https://example.org/c"),
                ]
            },
            {
                "results": [
                    source("https://example.org/a"),
                    source("https://example.org/a?x=2"),
                    source("https://example.org/d"),
                ]
            },
        ],
        3,
    )
    assert [r["url"] for r in result["results"]] == [
        "https://EXAMPLE.org:443/a",
        "https://example.org/a?x=1",
        "https://example.org/a?x=2",
    ]
    assert result["results"][0]["matched_queries"] == [0, 1]
    assert result["results_limited"] is True
    assert all(r["content_truncated"] for r in result["results"])
    assert sum(len(r["content"]) + len(r["raw_content"]) for r in result["results"]) <= 24000
    assert result["upstream_truncated"] is None


def test_mcp_rejects_removed_argument():
    from extensions.mcp_servers.internet_search_mcp.server import mcp
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        asyncio.run(mcp.call_tool("internet_search", {"query": "a", "cn_only": True}))


def test_two_mcp_sessions_share_capacity_and_close_client(monkeypatch):
    import json

    from extensions.mcp_servers.internet_search_mcp import impl, server
    from mcp import Client
    from tests.llm.test_mcp_parallel_execution import probe_server

    monkeypatch.setattr(
        impl,
        "get_runtime_value",
        lambda name: {"INTERNET_SEARCH_ENGINE": "tavily", "TAVILY_API_KEY": "test"}.get(name),
    )
    real_client = httpx.AsyncClient
    clients = []
    started = 0
    maximum = 0
    active = 0
    saturated = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        nonlocal started, maximum, active
        started += 1
        active += 1
        maximum = max(maximum, active)
        if active == 8:
            saturated.set()
        try:
            await release.wait()
            return httpx.Response(
                200, json={"results": [{"url": "https://example.org", "content": "proof"}]}
            )
        finally:
            active -= 1

    def client_factory(**kwargs):
        client = real_client(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(server.httpx, "AsyncClient", client_factory)

    async def run():
        async with probe_server(server.mcp) as (url, _):
            async with Client(url) as first, Client(url) as second:
                assert len(clients) == 1
                calls = [
                    asyncio.create_task(
                        first.call_tool(
                            "internet_search", {"queries": ["a", "b", "c", "d"], "max_results": 8}
                        )
                    ),
                    asyncio.create_task(
                        second.call_tool(
                            "internet_search", {"queries": ["e", "f", "g", "h"], "max_results": 8}
                        )
                    ),
                    asyncio.create_task(second.call_tool("internet_search", {"query": "i"})),
                ]
                await asyncio.wait_for(saturated.wait(), 2)
                await asyncio.sleep(0.01)
                assert started == 8
                release.set()
                results = await asyncio.gather(*calls)
                assert all(not result.is_error for result in results)
                assert maximum == 8
                data = json.loads(results[0].content[0].text)
                assert data["result"]["status"] == "complete"
                bad = await first.call_tool("internet_search", {"query": "a", "cn_only": True})
                assert bad.is_error
            assert not clients[0].is_closed
        assert clients[0].is_closed

    asyncio.run(run())


def test_oversize_search_response_is_a_query_failure(monkeypatch):
    from extensions.mcp_servers.internet_search_mcp import impl

    monkeypatch.setattr(
        impl,
        "get_runtime_value",
        lambda name: {"INTERNET_SEARCH_ENGINE": "tavily", "TAVILY_API_KEY": "test"}.get(name),
    )
    monkeypatch.setattr(impl, "MAX_RESPONSE_BYTES", 20)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 21))
        ) as client:
            result = await SearchService(client).search(SearchRequest(query="a"))
        assert result["status"] == "error"
        assert result["query_statuses"][0]["error_code"] == "response_too_large"

    asyncio.run(run())


def test_round_robin_caps_at_eight_sources():
    from extensions.mcp_servers.internet_search_mcp.merge import merge_sources

    batches = [
        {
            "results": [
                {"url": f"https://example.org/{q}{rank}", "content": "proof"}
                for rank in range(1, 4)
            ]
        }
        for q in "ABCD"
    ]
    result = merge_sources(batches, 8)
    assert [r["url"].rsplit("/", 1)[-1] for r in result["results"]] == [
        "A1",
        "B1",
        "C1",
        "D1",
        "A2",
        "B2",
        "C2",
        "D2",
    ]
    assert result["results_limited"]


def test_search_transport_allows_structured_deadline_result():
    from core.llm.mcp.client import _http_transport_timeout
    assert _http_transport_timeout("internet_search", {}, 120) == 50.0
    assert _http_transport_timeout("internet_search", {"transport_timeout": 12}, 120) == 12.0
