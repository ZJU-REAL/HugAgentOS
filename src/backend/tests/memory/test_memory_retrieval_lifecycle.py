import asyncio
from types import SimpleNamespace

import pytest

from core.memory.retrieval_types import MemoryRetrievalResult
from core.evolution import settlement_runner
from orchestration import memory_integration as M


@pytest.mark.asyncio
async def test_timed_out_retrieval_keeps_running_and_becomes_observable(monkeypatch):
    release = asyncio.Event()

    async def slow_retrieval():
        await release.wait()
        return MemoryRetrievalResult.degraded_result("late-result")

    async def empty_profile(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(M.profile, "get", empty_profile)
    monkeypatch.setattr(
        M,
        "settings",
        SimpleNamespace(memory=SimpleNamespace(retrieval_budget_ms=1)),
    )
    task = asyncio.create_task(slow_retrieval())
    M.track_memory_retrieval(task)

    block = await M.build_frozen_memory_block("u1", "default", task)

    assert block == ""
    assert task.done() is False
    assert task.cancelled() is False
    assert M.get_retrieval_state(task) == "timed_out_running"

    release.set()
    await task
    await asyncio.sleep(0)

    assert M.get_retrieval_state(task) == "completed_after_timeout"
    assert M.get_last_retrieval(task).degrade_reason == "late-result"


@pytest.mark.asyncio
async def test_launch_path_leaves_internal_timeout_unowned_and_continues(monkeypatch):
    release = asyncio.Event()
    observed_timeouts = []

    async def slow_retrieval(*_args, **kwargs):
        observed_timeouts.append(kwargs.get("timeout_s"))
        await release.wait()
        return MemoryRetrievalResult.degraded_result("launched-late")

    async def empty_profile(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(M, "retrieve_memories_structured", slow_retrieval)
    monkeypatch.setattr(M.profile, "get", empty_profile)
    task = await M.launch_memory_retrieval("u1", "hello", True, budget_ms=1)

    assert await M.build_frozen_memory_block("u1", "default", task) == ""
    assert observed_timeouts == [None]
    assert task.done() is False
    assert M.get_retrieval_state(task) == "timed_out_running"

    release.set()
    await task
    await asyncio.sleep(0)
    assert M.get_retrieval_state(task) == "completed_after_timeout"


@pytest.mark.asyncio
async def test_explicitly_cancelled_retrieval_records_cancelled():
    async def never_finishes():
        await asyncio.Event().wait()

    task = asyncio.create_task(never_finishes())
    M.track_memory_retrieval(task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert M.get_retrieval_state(task) == "cancelled"


def test_no_write_path_reports_empty_items_with_failure_state(monkeypatch):
    reports = []
    monkeypatch.setattr(
        settlement_runner,
        "report_memory_writes",
        lambda message_id, *, items, failed: reports.append((message_id, items, failed)),
    )

    M._report_no_memory_writes("m-no-write", failed=True)

    assert reports == [("m-no-write", [], True)]
