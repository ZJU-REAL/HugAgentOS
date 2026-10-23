"""The frozen memory block is built once per chat and replayed afterwards.

It is injected ahead of the entire conversation, so re-deriving it per turn
moves the prefix-cache boundary to the front of the prompt and costs a full
re-prefill of the whole context on every turn.
"""

from __future__ import annotations
from core.agent_runtime.integrations import memory as memory_module
from core.agent_runtime.integrations import memory_session as memory_session_module

import asyncio

import core.agent_runtime.integrations.memory as memory
import core.agent_runtime.integrations.memory as M
import pytest
from core.memory.retrieval_types import MemoryRetrievalResult, RetrievedMemory, content_hash
from core.memory.session_snapshot import SessionMemorySnapshot


def _fact(text: str) -> MemoryRetrievalResult:
    return MemoryRetrievalResult(
        items=(
            RetrievedMemory(
                memory_id="m1",
                layer="fact",
                content=text,
                content_hash=content_hash(text),
                score=1.0,
                adjusted_score=1.0,
                rank=1,
            ),
        ),
        recalled_count=1,
        filtered_count=1,
    )


@pytest.fixture
def store(monkeypatch):
    """In-memory stand-in for the ChatSession.extra_data snapshot column."""
    saved: dict = {}

    def _load(chat_id, *, scope_user_id, workspace_id):
        snapshot = saved.get(chat_id)
        if snapshot is None:
            return None
        if not snapshot.matches(scope_user_id=scope_user_id, workspace_id=workspace_id):
            return None
        return snapshot

    def _save(chat_id, snapshot):
        # First writer wins, mirroring the locked read-modify-write in the real store.
        stored = saved.get(chat_id)
        if stored is not None and stored.matches(
            scope_user_id=snapshot.scope_user_id, workspace_id=snapshot.workspace_id
        ):
            return stored
        saved[chat_id] = snapshot
        return snapshot

    monkeypatch.setattr(memory_session_module, "load_session_memory_snapshot", _load)
    monkeypatch.setattr(memory_session_module, "save_session_memory_snapshot", _save)
    return saved


@pytest.fixture
def retrievals(monkeypatch):
    """Count every vector search the orchestration layer issues."""
    calls: list = []

    async def _retrieve(*, user_id, query, workspace_id, timeout_s):
        calls.append(query)
        return _fact(f"记忆-{len(calls)}")

    async def _profile(*_args, **_kwargs):
        return "- 用户偏好：简洁"

    monkeypatch.setattr(memory, "retrieve_memories_structured", _retrieve)
    monkeypatch.setattr(M.profile, "get", _profile)
    return calls


async def _turn(chat_id: str, message: str) -> str:
    handle = await memory_session_module.open_session_memory(
        chat_id=chat_id,
        scope_user_id="u1",
        workspace_id="default",
        user_message=message,
        memory_enabled=True,
    )
    return await memory_session_module.resolve_session_memory(handle)


@pytest.mark.asyncio
async def test_first_turn_builds_and_stores_the_snapshot(store, retrievals):
    block = await _turn("chat-1", "第一问")

    assert "记忆-1" in block
    assert retrievals == ["第一问"]
    assert store["chat-1"].text == block
    assert store["chat-1"].scope_user_id == "u1"


@pytest.mark.asyncio
async def test_later_turns_replay_the_block_without_retrieving(store, retrievals):
    first = await _turn("chat-1", "第一问")
    second = await _turn("chat-1", "完全不同的第二问")
    third = await _turn("chat-1", "第三问")

    assert second == first == third
    # The whole point: only the first turn ever queries the store.
    assert retrievals == ["第一问"]


@pytest.mark.asyncio
async def test_replay_turn_exposes_no_retrieval_task(store, retrievals):
    await _turn("chat-1", "第一问")
    handle = await memory_session_module.open_session_memory(
        chat_id="chat-1",
        scope_user_id="u1",
        workspace_id="default",
        user_message="第二问",
        memory_enabled=True,
    )

    # No read happened this turn, so the episode must not claim memory evidence.
    assert handle.retrieval_task is None
    assert memory_module.get_last_retrieval(handle.retrieval_task) is None


@pytest.mark.asyncio
async def test_each_chat_freezes_its_own_snapshot(store, retrievals):
    await _turn("chat-1", "第一问")
    await _turn("chat-2", "另一个会话的第一问")

    assert retrievals == ["第一问", "另一个会话的第一问"]
    assert store["chat-1"].text != store["chat-2"].text


@pytest.mark.asyncio
async def test_a_snapshot_from_another_scope_is_not_served(store, retrievals):
    await _turn("chat-1", "第一问")
    store["chat-1"] = SessionMemorySnapshot(
        text=store["chat-1"].text,
        scope_user_id="team:t1",
        workspace_id="default",
    )

    await _turn("chat-1", "第二问")

    assert retrievals == ["第一问", "第二问"]


@pytest.mark.asyncio
async def test_empty_memory_is_frozen_too(store, monkeypatch):
    async def _retrieve(*, user_id, query, workspace_id, timeout_s):
        return MemoryRetrievalResult()

    async def _profile(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(memory, "retrieve_memories_structured", _retrieve)
    monkeypatch.setattr(M.profile, "get", _profile)

    assert await _turn("chat-1", "第一问") == ""
    # "nothing known about this user" is a real answer and must not be re-asked.
    assert store["chat-1"].text == ""


@pytest.mark.asyncio
async def test_a_degraded_read_is_not_frozen(store, monkeypatch):
    async def _degraded(*, user_id, query, workspace_id, timeout_s):
        return MemoryRetrievalResult.degraded_result("store-unreachable")

    async def _profile(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(memory, "retrieve_memories_structured", _degraded)
    monkeypatch.setattr(M.profile, "get", _profile)

    await _turn("chat-1", "第一问")

    # Freezing a failed read would blind the chat's memory for good.
    assert "chat-1" not in store


@pytest.mark.asyncio
async def test_a_timed_out_read_is_not_frozen(store, monkeypatch):
    release = asyncio.Event()

    async def _slow(*, user_id, query, workspace_id, timeout_s):
        await release.wait()
        return _fact("迟到的记忆")

    async def _profile(*_args, **_kwargs):
        return ""

    monkeypatch.setattr(memory, "retrieve_memories_structured", _slow)
    monkeypatch.setattr(M.profile, "get", _profile)

    handle = await memory_session_module.open_session_memory(
        chat_id="chat-1",
        scope_user_id="u1",
        workspace_id="default",
        user_message="第一问",
        memory_enabled=True,
        budget_ms=1,
    )
    assert await memory_session_module.resolve_session_memory(handle) == ""
    assert "chat-1" not in store

    release.set()
    await handle.retrieval_task


@pytest.mark.asyncio
async def test_memory_off_neither_retrieves_nor_stores(store, retrievals):
    handle = await memory_session_module.open_session_memory(
        chat_id="chat-1",
        scope_user_id="u1",
        workspace_id="default",
        user_message="第一问",
        memory_enabled=False,
    )

    assert await memory_session_module.resolve_session_memory(handle) == ""
    assert handle.retrieval_task is None
    assert retrievals == []
    assert store == {}


@pytest.mark.asyncio
async def test_concurrent_first_turns_converge_on_one_snapshot(store, retrievals):
    """Two runs opening the same chat at once must not disagree about what it froze."""
    a, b = await asyncio.gather(_turn("chat-1", "并发A"), _turn("chat-1", "并发B"))

    assert a == b, "并发的两轮必须注入同一份快照"
    assert store["chat-1"].text == a
    # A third turn replays the same snapshot and issues no further search.
    before = len(retrievals)
    assert await _turn("chat-1", "之后") == a
    assert len(retrievals) == before
