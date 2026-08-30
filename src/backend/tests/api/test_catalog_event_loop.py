"""Regression tests for catalog event-loop safety and shared provider caching."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from api.routes.v1 import catalog


@pytest.fixture(autouse=True)
def _reset_external_cache(monkeypatch):
    monkeypatch.setattr(catalog, "_external_cache", {})
    monkeypatch.setattr(catalog, "_external_cache_refreshing", set())
    monkeypatch.setattr(catalog, "_external_cache_async_lock", None)
    monkeypatch.setattr(catalog, "_external_cache_async_lock_loop", None)
    monkeypatch.setattr(catalog, "get_provider_name", lambda: "dify")
    monkeypatch.setattr(
        catalog,
        "get_provider_cache_identity",
        lambda provider_name: f"{provider_name}:default",
        raising=False,
    )


@pytest.mark.asyncio
async def test_catalog_handler_offloads_the_synchronous_builder(monkeypatch):
    user = SimpleNamespace(user_id="user-1")
    db = object()
    expected = {"data": {"kb": []}}
    calls: List[tuple[Any, tuple[Any, ...]]] = []

    async def cache_ready() -> None:
        return None

    def builder(received_user, received_db):
        pytest.fail("the builder must be passed to the threadpool, not called directly")

    async def run_in_threadpool(function, *args):
        calls.append((function, args))
        return expected

    monkeypatch.setattr(catalog, "_ensure_external_cache_ready", cache_ready)
    monkeypatch.setattr(catalog, "_get_catalog_items_sync", builder)
    monkeypatch.setattr(catalog, "run_in_threadpool", run_in_threadpool)

    result = await catalog.get_catalog_items(user=user, db=db)

    assert result is expected
    assert calls == [(builder, (user, db))]


@pytest.mark.asyncio
async def test_catalog_build_does_not_block_the_event_loop(monkeypatch):
    async def cache_ready() -> None:
        return None

    def slow_builder(_user, _db):
        time.sleep(0.08)
        return "done"

    monkeypatch.setattr(catalog, "_ensure_external_cache_ready", cache_ready)
    monkeypatch.setattr(catalog, "_get_catalog_items_sync", slow_builder)

    request = asyncio.create_task(catalog.get_catalog_items(user=object(), db=object()))
    ticks = 0
    while not request.done():
        ticks += 1
        await asyncio.sleep(0.005)

    assert await request == "done"
    assert ticks >= 3, "the event loop must keep scheduling while catalog work is running"


@pytest.mark.asyncio
async def test_cold_catalog_requests_share_one_nonblocking_refresh(monkeypatch):
    provider_calls = 0
    count_lock = threading.Lock()

    def slow_provider(
        *, provider_name: str, page: int, limit: int, raise_on_error: bool
    ) -> List[Dict[str, Any]]:
        nonlocal provider_calls
        assert provider_name == "dify"
        assert (page, limit, raise_on_error) == (1, 100, True)
        with count_lock:
            provider_calls += 1
        time.sleep(0.08)
        return [{"id": "fresh"}]

    monkeypatch.setattr(catalog, "list_collections", slow_provider)
    monkeypatch.setattr(
        catalog,
        "_prepare_external_cache_sync",
        lambda: catalog._list_datasets_cached(),
    )

    refreshes = asyncio.gather(
        catalog._ensure_external_cache_ready(),
        catalog._ensure_external_cache_ready(),
    )
    ticks = 0
    while not refreshes.done():
        ticks += 1
        await asyncio.sleep(0.005)
    await refreshes

    assert provider_calls == 1
    assert ticks >= 3, "followers must wait without blocking the event loop"
    assert catalog._list_datasets_cached() == [{"id": "fresh"}]


@pytest.mark.asyncio
async def test_concurrent_cold_catalog_handlers_both_receive_the_fresh_snapshot(monkeypatch):
    provider_calls = 0

    def slow_provider(
        *, provider_name: str, page: int, limit: int, raise_on_error: bool
    ) -> List[Dict[str, Any]]:
        nonlocal provider_calls
        assert provider_name == "dify"
        assert (page, limit, raise_on_error) == (1, 100, True)
        provider_calls += 1
        time.sleep(0.08)
        return [{"id": "fresh"}]

    def builder(_user, _db):
        return catalog._list_datasets_cached()

    monkeypatch.setattr(catalog, "list_collections", slow_provider)
    monkeypatch.setattr(
        catalog,
        "_prepare_external_cache_sync",
        lambda: catalog._list_datasets_cached(),
    )
    monkeypatch.setattr(catalog, "_get_catalog_items_sync", builder)

    requests = asyncio.gather(
        catalog.get_catalog_items(user=object(), db=object()),
        catalog.get_catalog_items(user=object(), db=object()),
    )
    deadline = asyncio.get_running_loop().time() + 2
    while not requests.done():
        assert asyncio.get_running_loop().time() < deadline, "concurrent catalog requests timed out"
        await asyncio.sleep(0.005)
    first, second = await requests

    assert first == second == [{"id": "fresh"}]
    assert provider_calls == 1


def test_external_cache_returns_isolated_copies(monkeypatch):
    provider_calls = 0

    def provider(
        *, provider_name: str, page: int, limit: int, raise_on_error: bool
    ) -> List[Dict[str, Any]]:
        nonlocal provider_calls
        assert provider_name == "dify"
        assert (page, limit, raise_on_error) == (1, 100, True)
        provider_calls += 1
        return [{"id": "dataset", "metadata": {"scope": "public"}}]

    monkeypatch.setattr(catalog, "list_collections", provider)

    first = catalog._list_datasets_cached()
    first[0]["access_level"] = "admin"
    first[0]["metadata"]["scope"] = "private"

    assert catalog._list_datasets_cached() == [{"id": "dataset", "metadata": {"scope": "public"}}]
    assert provider_calls == 1


def test_external_cache_is_scoped_to_the_active_provider(monkeypatch):
    active_provider = "dify"
    provider_calls: List[str] = []

    def provider(
        *, provider_name: str, page: int, limit: int, raise_on_error: bool
    ) -> List[Dict[str, Any]]:
        assert (page, limit, raise_on_error) == (1, 100, True)
        provider_calls.append(provider_name)
        return [{"id": f"{provider_name}-dataset"}]

    monkeypatch.setattr(catalog, "get_provider_name", lambda: active_provider)
    monkeypatch.setattr(
        catalog,
        "get_provider_cache_identity",
        lambda provider_name: f"{provider_name}:default",
        raising=False,
    )
    monkeypatch.setattr(catalog, "list_collections", provider)

    assert catalog._list_datasets_cached() == [{"id": "dify-dataset"}]
    active_provider = "fastgpt"
    assert catalog._list_datasets_cached() == [{"id": "fastgpt-dataset"}]
    assert provider_calls == ["dify", "fastgpt"]


def test_external_cache_is_scoped_to_the_provider_configuration(monkeypatch):
    active_config = "endpoint-a"
    provider_calls: List[str] = []

    def provider(
        *, provider_name: str, page: int, limit: int, raise_on_error: bool
    ) -> List[Dict[str, Any]]:
        assert provider_name == "dify"
        assert (page, limit, raise_on_error) == (1, 100, True)
        provider_calls.append(active_config)
        return [{"id": f"{active_config}-dataset"}]

    monkeypatch.setattr(
        catalog,
        "get_provider_cache_identity",
        lambda provider_name: f"{provider_name}:{active_config}",
        raising=False,
    )
    monkeypatch.setattr(catalog, "list_collections", provider)

    assert catalog._list_datasets_cached() == [{"id": "endpoint-a-dataset"}]
    active_config = "endpoint-b"
    assert catalog._list_datasets_cached() == [{"id": "endpoint-b-dataset"}]
    assert provider_calls == ["endpoint-a", "endpoint-b"]


def test_external_cache_prunes_expired_inactive_configurations(monkeypatch):
    current_identity = catalog.get_provider_cache_identity("dify")
    catalog._external_cache = {
        current_identity: (time.monotonic() + 30, [{"id": "current"}]),
        "dify:expired": (0.0, [{"id": "expired"}]),
    }

    assert catalog._list_datasets_cached() == [{"id": "current"}]
    assert "dify:expired" not in catalog._external_cache


def test_external_cache_pins_the_provider_during_a_config_switch(monkeypatch):
    active_provider = "dify"
    switch_pending = True
    provider_calls: List[str] = []

    def provider(
        *, provider_name: str, page: int, limit: int, raise_on_error: bool
    ) -> List[Dict[str, Any]]:
        nonlocal active_provider, switch_pending
        assert (page, limit, raise_on_error) == (1, 100, True)
        provider_calls.append(provider_name)
        if provider_name == "dify" and switch_pending:
            switch_pending = False
            active_provider = "fastgpt"
        return [{"id": f"{provider_name}-dataset"}]

    monkeypatch.setattr(catalog, "get_provider_name", lambda: active_provider)
    monkeypatch.setattr(
        catalog,
        "get_provider_cache_identity",
        lambda provider_name: f"{provider_name}:default",
        raising=False,
    )
    monkeypatch.setattr(catalog, "list_collections", provider)

    # The provider changed while the old request was in flight, so discard the old
    # snapshot and retry against the now-current provider before returning.
    assert catalog._list_datasets_cached() == [{"id": "fastgpt-dataset"}]
    active_provider = "dify"
    assert catalog._list_datasets_cached() == [{"id": "dify-dataset"}]
    assert provider_calls == ["dify", "fastgpt", "dify"]


def test_external_cache_serves_stale_data_during_failure_backoff(monkeypatch):
    provider_calls = 0

    def unavailable_provider(
        *, provider_name: str, page: int, limit: int, raise_on_error: bool
    ) -> List[Dict[str, Any]]:
        nonlocal provider_calls
        assert provider_name == "dify"
        assert (page, limit, raise_on_error) == (1, 100, True)
        provider_calls += 1
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(catalog, "list_collections", unavailable_provider)
    provider = catalog.get_provider_name() or "dify"
    identity = catalog.get_provider_cache_identity(provider)
    catalog._external_cache[identity] = (0.0, [{"id": "stale"}])

    first = catalog._list_datasets_cached()
    second = catalog._list_datasets_cached()

    assert first == second == [{"id": "stale"}]
    assert provider_calls == 1
