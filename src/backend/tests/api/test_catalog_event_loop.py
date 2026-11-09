"""Catalog handler: event-loop safety and how it reads the shared knowledge directory.

The directory's own freshness / single-flight / scoping rules are covered in
``tests/edition_ee/test_kb_directory.py``.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, List

import pytest
from api.routes.v1 import catalog


@pytest.fixture(autouse=True)
def _ee_with_external_provider(monkeypatch):
    monkeypatch.setattr(catalog, "settings", SimpleNamespace(edition=SimpleNamespace(edition="ee")))
    monkeypatch.setattr(catalog, "is_enabled", lambda: True)


@pytest.mark.asyncio
async def test_catalog_handler_offloads_the_synchronous_builder(monkeypatch):
    user = SimpleNamespace(user_id="user-1")
    expected = {"data": {"kb": []}}
    built: List[tuple[Any, Any, Any]] = []
    offloaded: List[Any] = []

    async def collections():
        return [{"id": "ext"}]

    def builder(received_user, received_db, external_items):
        built.append((received_user, received_db, external_items))
        return expected

    async def run_in_session(function, *args):
        # The builder only ever runs inside the session step handed to the thread.
        assert built == []
        offloaded.append(function)
        return function("session")

    monkeypatch.setattr(catalog, "get_collections_async", collections)
    monkeypatch.setattr(catalog, "_get_catalog_items_sync", builder)
    monkeypatch.setattr(catalog, "run_in_session", run_in_session)

    result = await catalog.get_catalog_items(user=user)

    assert result is expected
    assert len(offloaded) == 1 and built == [(user, "session", [{"id": "ext"}])]


@pytest.mark.asyncio
async def test_catalog_build_does_not_block_the_event_loop(monkeypatch):
    async def collections():
        return []

    def slow_builder(_user, _db, _external):
        time.sleep(0.08)
        return "done"

    monkeypatch.setattr(catalog, "get_collections_async", collections)
    monkeypatch.setattr(catalog, "_get_catalog_items_sync", slow_builder)

    request = asyncio.create_task(catalog.get_catalog_items(user=object()))
    ticks = 0
    while not request.done():
        ticks += 1
        await asyncio.sleep(0.005)

    assert await request == "done"
    assert ticks >= 3, "the event loop must keep scheduling while catalog work is running"


@pytest.mark.asyncio
async def test_unavailable_directory_still_returns_the_local_catalog(monkeypatch):
    async def unavailable():
        raise RuntimeError("provider down")

    received: List[Any] = []
    monkeypatch.setattr(catalog, "get_collections_async", unavailable)
    monkeypatch.setattr(
        catalog, "_get_catalog_items_sync", lambda _u, _db, external: received.append(external) or "ok"
    )

    assert await catalog.get_catalog_items(user=object()) == "ok"
    assert received == [[]]


@pytest.mark.asyncio
@pytest.mark.parametrize("edition, enabled", [("ce", True), ("ee", False)])
async def test_directory_is_not_read_without_an_external_provider(monkeypatch, edition, enabled):
    async def must_not_run():
        raise AssertionError("directory read without an external provider")

    monkeypatch.setattr(catalog, "settings", SimpleNamespace(edition=SimpleNamespace(edition=edition)))
    monkeypatch.setattr(catalog, "is_enabled", lambda: enabled)
    monkeypatch.setattr(catalog, "get_collections_async", must_not_run)

    assert await catalog._external_collections() == []
