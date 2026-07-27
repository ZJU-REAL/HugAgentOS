"""Tests for the provider-neutral internet search MCP implementation."""

from __future__ import annotations

import asyncio

import pytest
from core.services import service_probes, system_config
from mcp_servers.internet_search_mcp import impl


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = "fake response"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class _FakeSyncClient:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    def post(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response


class _FakeAsyncClient:
    calls: list[dict] = []
    response = _FakeResponse({"code": 200, "msg": "ok"})

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, **kwargs):
        self.__class__.calls.append({"url": url, **kwargs})
        return self.__class__.response


def test_normalize_langsearch_response_prefers_summary_and_keeps_date() -> None:
    data = {
        "data": {
            "webPages": {
                "value": [
                    {
                        "name": "Result A",
                        "url": "https://example.com/a",
                        "snippet": "short A",
                        "summary": "summary A",
                        "datePublished": "2026-07-26T00:00:00Z",
                    },
                    {
                        "name": "Result B",
                        "url": "https://example.com/b",
                        "snippet": "short B",
                        "summary": "",
                    },
                    {"name": "Result C", "url": "https://example.com/c"},
                ]
            }
        }
    }

    result = impl._normalize_langsearch_response(data, max_results=2)

    assert result == {
        "results": [
            {
                "title": "Result A",
                "url": "https://example.com/a",
                "content": "summary A",
                "published_date": "2026-07-26T00:00:00Z",
            },
            {
                "title": "Result B",
                "url": "https://example.com/b",
                "content": "short B",
            },
        ]
    }


def test_langsearch_search_sends_expected_request_and_clamps_count(monkeypatch) -> None:
    response = _FakeResponse({"code": 200, "data": {"webPages": {"value": []}}})
    client = _FakeSyncClient(response)
    monkeypatch.setattr(impl, "_get_httpx_client", lambda: client)
    monkeypatch.setattr(
        impl,
        "get_runtime_value",
        lambda name: "test-langsearch-key" if name == "LANGSEARCH_API_KEY" else None,
    )

    result = impl._langsearch_search("open source agent", max_results=99)

    assert result == {"results": []}
    assert client.calls == [
        {
            "url": impl.LANGSEARCH_SEARCH_URL,
            "headers": {
                "Authorization": "Bearer test-langsearch-key",
                "Content-Type": "application/json",
            },
            "json": {
                "query": "open source agent",
                "freshness": "noLimit",
                "summary": True,
                "count": 10,
            },
        }
    ]


def test_internet_search_routes_to_langsearch(monkeypatch) -> None:
    expected = {"results": [{"title": "A", "url": "https://example.com", "content": "B"}]}
    monkeypatch.setattr(
        impl,
        "_env_str",
        lambda name, default: "langsearch" if name == "INTERNET_SEARCH_ENGINE" else default,
    )
    monkeypatch.setattr(impl, "_langsearch_search", lambda query, max_results: expected)
    monkeypatch.setattr(impl, "safe_stream_writer", lambda: lambda message: None)

    result = impl.internet_search("test", max_results=3, cn_only=False)

    assert result is expected


def test_internet_search_rejects_unknown_engine(monkeypatch) -> None:
    monkeypatch.setattr(impl, "_env_str", lambda name, default: "typo")
    monkeypatch.setattr(impl, "safe_stream_writer", lambda: lambda message: None)

    with pytest.raises(RuntimeError, match="Unsupported internet search engine"):
        impl.internet_search("test", cn_only=False)


def test_langsearch_connectivity_check_validates_api_payload(monkeypatch) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = _FakeResponse({"code": 200, "msg": "ok"})
    monkeypatch.setattr(service_probes.httpx, "AsyncClient", _FakeAsyncClient)

    result = asyncio.run(service_probes.test_langsearch("test-key"))

    assert result["success"] is True
    assert result["error"] is None
    assert _FakeAsyncClient.calls[0] == {
        "url": service_probes.LANGSEARCH_SEARCH_API_URL,
        "headers": {
            "Authorization": "Bearer test-key",
            "Content-Type": "application/json",
        },
        "json": {
            "query": "test",
            "freshness": "noLimit",
            "summary": False,
            "count": 1,
        },
    }


def test_langsearch_connectivity_check_rejects_api_error(monkeypatch) -> None:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = _FakeResponse({"code": 401, "msg": "invalid key"})
    monkeypatch.setattr(service_probes.httpx, "AsyncClient", _FakeAsyncClient)

    result = asyncio.run(service_probes.test_langsearch("bad-key"))

    assert result["success"] is False
    assert "invalid key" in result["error"]


def test_service_group_uses_only_selected_engine_key(monkeypatch) -> None:
    class _FakeConfigService:
        values = {
            "internet_search.engine": "langsearch",
            "internet_search.tavily_api_key": "wrong-tavily-key",
            "internet_search.langsearch_api_key": "selected-langsearch-key",
        }

        def get(self, key: str):
            return self.values.get(key)

    captured: list[str] = []

    async def _fake_test_langsearch(api_key: str) -> dict:
        captured.append(api_key)
        return {"success": True, "latency_ms": 1, "error": None}

    monkeypatch.setattr(
        system_config.SystemConfigService,
        "get_instance",
        classmethod(lambda cls: _FakeConfigService()),
    )
    monkeypatch.setattr(service_probes, "test_langsearch", _fake_test_langsearch)

    result = asyncio.run(service_probes.test_service_group("internet_search"))

    assert result["success"] is True
    assert captured == ["selected-langsearch-key"]


def test_langsearch_has_an_independent_system_config_key() -> None:
    seed_keys = {item[0] for item in system_config.SEED_CONFIGS}

    assert "internet_search.langsearch_api_key" in seed_keys
    assert (
        system_config.get_config_key_for_env("LANGSEARCH_API_KEY")
        == "internet_search.langsearch_api_key"
    )
    assert system_config.get_config_key_for_env("TAVILY_API_KEY") == (
        "internet_search.tavily_api_key"
    )
