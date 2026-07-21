from __future__ import annotations

import asyncio
import time

import pytest


class _FakeResponse:
    def __init__(self, dataset_id: str) -> None:
        self._dataset_id = dataset_id

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "records": [
                {
                    "segment": {
                        "content": f"content-{self._dataset_id}",
                        "tokens": 1,
                        "document": {
                            "id": f"doc-{self._dataset_id}",
                            "name": f"document-{self._dataset_id}",
                        },
                    }
                }
            ]
        }


class _FakeAsyncClient:
    active = 0
    max_active = 0
    delay = 0.02

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def post(self, url: str, **_kwargs) -> _FakeResponse:
        type(self).active += 1
        type(self).max_active = max(type(self).max_active, type(self).active)
        try:
            await asyncio.sleep(type(self).delay)
        finally:
            type(self).active -= 1
        return _FakeResponse(url.rsplit("/", 2)[-2])


@pytest.mark.asyncio
async def test_public_retrieval_is_nonblocking_and_globally_bounded(monkeypatch):
    from mcp_servers.retrieve_dataset_content_mcp import impl

    monkeypatch.setattr(impl, "RETRIEVE_MAX_CONCURRENCY", 2)
    monkeypatch.setattr(impl, "RETRIEVE_TOTAL_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(impl.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        impl,
        "_resolve_public_retrieve_scope",
        lambda **_kwargs: ("http://dify.test", "token", {f"dataset-{i}" for i in range(6)}),
    )
    impl._public_retrieve_limiter = None
    impl._public_retrieve_limiter_loop = None
    _FakeAsyncClient.active = 0
    _FakeAsyncClient.max_active = 0
    _FakeAsyncClient.delay = 0.02

    retrieval = asyncio.gather(
        impl.retrieve_dataset_content_async(query="first"),
        impl.retrieve_dataset_content_async(query="second"),
    )
    ticks = 0
    while not retrieval.done():
        ticks += 1
        await asyncio.sleep(0.005)

    first_items, second_items = await retrieval
    assert len(first_items) == 6
    assert len(second_items) == 6
    assert _FakeAsyncClient.max_active == 2
    assert ticks >= 3, "event loop should remain responsive while Dify requests are in flight"


@pytest.mark.asyncio
async def test_public_retrieval_has_one_end_to_end_deadline(monkeypatch):
    from mcp_servers.retrieve_dataset_content_mcp import impl

    monkeypatch.setattr(impl, "RETRIEVE_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(impl, "RETRIEVE_TOTAL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(impl.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(
        impl,
        "_resolve_public_retrieve_scope",
        lambda **_kwargs: ("http://dify.test", "token", {"dataset-1"}),
    )
    impl._public_retrieve_limiter = None
    impl._public_retrieve_limiter_loop = None
    _FakeAsyncClient.active = 0
    _FakeAsyncClient.max_active = 0
    _FakeAsyncClient.delay = 1.0

    started = time.monotonic()
    with pytest.raises(impl.DatasetRetrievalTimeoutError):
        await impl.retrieve_dataset_content_async(query="test")
    assert time.monotonic() - started < 0.25


@pytest.mark.asyncio
async def test_all_public_upstream_failures_are_not_reported_as_empty_results(monkeypatch):
    from mcp_servers.retrieve_dataset_content_mcp import impl

    class _FailingClient(_FakeAsyncClient):
        async def post(self, _url: str, **_kwargs):
            raise OSError("upstream unavailable")

    monkeypatch.setattr(impl, "RETRIEVE_MAX_CONCURRENCY", 2)
    monkeypatch.setattr(impl, "RETRIEVE_TOTAL_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(impl.httpx, "AsyncClient", _FailingClient)
    monkeypatch.setattr(
        impl,
        "_resolve_public_retrieve_scope",
        lambda **_kwargs: ("http://dify.test", "token", {"dataset-1", "dataset-2"}),
    )
    impl._public_retrieve_limiter = None
    impl._public_retrieve_limiter_loop = None

    with pytest.raises(impl.DatasetRetrievalUnavailableError, match="全部 2 个"):
        await impl.retrieve_dataset_content_async(query="test")


@pytest.mark.asyncio
async def test_blocking_lane_keeps_slot_until_timed_out_thread_finishes():
    from mcp_servers.retrieve_dataset_content_mcp.server import _BlockingLane

    lane = _BlockingLane(name="test", max_workers=1)

    with pytest.raises(TimeoutError):
        await lane.run(lambda: time.sleep(0.08), timeout=0.02)

    # The first caller timed out, but its thread is still alive. A new caller
    # must not be admitted into another unbounded executor queue.
    with pytest.raises(TimeoutError):
        await lane.run(lambda: "too-early", timeout=0.02)

    await asyncio.sleep(0.07)
    assert await lane.run(lambda: "done", timeout=0.05) == "done"


@pytest.mark.asyncio
async def test_cancelling_blocking_caller_does_not_release_a_live_thread_slot():
    from mcp_servers.retrieve_dataset_content_mcp.server import _BlockingLane

    lane = _BlockingLane(name="cancel-test", max_workers=1)
    task = asyncio.create_task(lane.run(lambda: time.sleep(0.08), timeout=1.0))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(TimeoutError):
        await lane.run(lambda: "too-early", timeout=0.02)

    await asyncio.sleep(0.07)
    assert await lane.run(lambda: "done", timeout=0.05) == "done"


@pytest.mark.asyncio
async def test_public_timeout_is_returned_as_a_tool_result(monkeypatch):
    from mcp_servers.retrieve_dataset_content_mcp import impl, server

    async def _timeout(**_kwargs):
        raise impl.DatasetRetrievalTimeoutError("public retrieval deadline")

    monkeypatch.setattr(impl, "retrieve_dataset_content_async", _timeout)
    result = await server.retrieve_dataset_content(query="test")

    assert result["items"] == []
    assert result["error"]["code"] == "tool_timeout"
    assert result["error"]["tool"] == "retrieve_dataset_content"
    assert result["error"]["retryable"] is True
