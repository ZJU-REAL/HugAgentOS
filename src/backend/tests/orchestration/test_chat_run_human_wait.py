"""ChatRun liveness and replay-retention contract for human waits."""

import asyncio

import pytest

from core.llm import human_interaction
from orchestration import chat_run_executor


def test_stream_ttl_covers_maximum_human_wait_plus_recovery_grace():
    assert (
        chat_run_executor._STREAM_TTL_SECONDS
        == human_interaction.MAX_WAIT_SECONDS
        + human_interaction.STREAM_RECOVERY_GRACE_SECONDS
    )
    assert chat_run_executor._STREAM_TTL_SECONDS > human_interaction.MAX_WAIT_SECONDS


def test_only_a_live_human_wait_turns_heartbeat_into_meaningful_activity(monkeypatch):
    monkeypatch.setattr(human_interaction, "has_pending", lambda _chat_id: False)
    assert chat_run_executor._is_chat_run_activity(
        {"type": "heartbeat"},
        "chat-1",
    ) is False
    assert chat_run_executor._is_chat_run_activity(
        {"type": "content"},
        "chat-1",
    ) is True

    monkeypatch.setattr(human_interaction, "has_pending", lambda _chat_id: True)
    assert chat_run_executor._is_chat_run_activity(
        {"type": "heartbeat"},
        "chat-1",
    ) is True


@pytest.mark.asyncio
async def test_human_wait_heartbeat_keeps_watchdog_alive(monkeypatch):
    async def delayed_content():
        for _ in range(5):
            await asyncio.sleep(0.01)
            yield {"type": "heartbeat"}
        yield {"type": "content", "delta": "done"}

    monkeypatch.setattr(human_interaction, "has_pending", lambda _chat_id: True)
    items = []
    async for item in chat_run_executor._aiter_with_inactivity_timeout(
        delayed_content(),
        0.02,
        is_activity=lambda item: chat_run_executor._is_chat_run_activity(
            item,
            "chat-waiting",
        ),
    ):
        items.append(item)
    assert items[-1]["type"] == "content"


@pytest.mark.asyncio
async def test_transport_heartbeat_without_human_wait_still_times_out(monkeypatch):
    async def only_heartbeats():
        while True:
            await asyncio.sleep(0.005)
            yield {"type": "heartbeat"}

    monkeypatch.setattr(human_interaction, "has_pending", lambda _chat_id: False)
    with pytest.raises(TimeoutError, match="无有效输出"):
        async for _ in chat_run_executor._aiter_with_inactivity_timeout(
            only_heartbeats(),
            0.02,
            is_activity=lambda item: chat_run_executor._is_chat_run_activity(
                item,
                "chat-not-waiting",
            ),
        ):
            pass
