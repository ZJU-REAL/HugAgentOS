"""Tests for the provider-neutral internet search MCP implementation."""

from __future__ import annotations

import asyncio

import pytest
from core.services import service_probes, system_config


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
