"""Measured post-turn usage must not be vetoed by the byte estimator."""

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import sessionmaker

import core.db.engine as engine_mod
from core.config import settings as config_settings
from core.db.models import ChatSession
from core.llm.context import compaction as C
from core.llm.context import budget as budget
from core.chat.compaction.coordinator import CompactionCoordinator
from core.chat.service import ChatService

INPUTS = {
    "system_prompt": "",
    "context_window": 36000,
    "model_name": "qwen",
    "model_provider_id": "test-provider",
}
USAGE = {
    "schema_version": "context-usage.v2",
    "source": "provider",
    "exact": True,
    "used_tokens": 40000,
    "captured_at_ms": 1000,
    **INPUTS,
}


def test_measured_threshold_publishes_checkpoint_despite_lower_estimate(db_session, monkeypatch):
    db_session.add(ChatSession(chat_id="measured-compaction", user_id="u1", title="test"))
    db_session.commit()
    svc = ChatService(db_session)
    for message_id, role, content, extra in [
        ("measured-user", "user", "合成" * 50, {}),
        ("measured-answer", "assistant", "ok", {"context_usage": USAGE}),
    ]:
        svc.message_repo.create(
            {
                "message_id": message_id,
                "chat_id": "measured-compaction",
                "role": role,
                "content": content,
                "extra_data": extra,
            }
        )
    monkeypatch.setattr(engine_mod, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    cfg = dataclasses.replace(
        config_settings.settings.compaction, enabled=True, keep_recent_tokens=1
    )
    monkeypatch.setattr(
        config_settings, "settings", dataclasses.replace(config_settings.settings, compaction=cfg)
    )
    called = []

    async def summarize(history, *, timeout):
        called.append(history)
        return "synthetic summary"

    monkeypatch.setattr('core.chat.compaction.summary._summarize', summarize)
    result = asyncio.run(
        CompactionCoordinator("measured-compaction").compact(
            budget_inputs=INPUTS, token_limit=29520
        )
    )
    assert result is not None and called
    db_session.expire_all()
    checkpoint = svc.get_latest_compaction_checkpoint("measured-compaction")
    assert checkpoint.extra_data["replacement_manifest"]["phase"] == "post_turn"


@pytest.mark.parametrize(
    "change",
    [
        {"source": "backend_estimate"},
        {"exact": False},
        {"model_provider_id": "other"},
        {"context_window": 128000},
        {"captured_at_ms": 0},
        {"used_tokens": True},
    ],
)
def test_mismatched_or_untrusted_measurement_keeps_estimate(change):
    row = SimpleNamespace(
        role="assistant", chat_seq=2, extra_data={"context_usage": {**USAGE, **change}}
    )
    snapshot = SimpleNamespace(source_rows=(row,), covered_seq=2)
    assert budget.resolve_snapshot_active_tokens(snapshot, INPUTS, 200) == 200


def test_only_final_uncovered_assistant_measurement_is_used():
    row = SimpleNamespace(role="assistant", chat_seq=2, extra_data={"context_usage": USAGE})
    snapshot = SimpleNamespace(source_rows=(row,), covered_seq=2)
    assert budget.resolve_snapshot_active_tokens(snapshot, INPUTS, 200) == 40000
    snapshot.source_rows = (row, SimpleNamespace(role="user", chat_seq=3, extra_data={}))
    snapshot.covered_seq = 3
    assert budget.resolve_snapshot_active_tokens(snapshot, INPUTS, 200) == 200
    snapshot.source_rows = ()  # An existing checkpoint already covers that measurement.
    assert budget.resolve_snapshot_active_tokens(snapshot, INPUTS, 200) == 200


@pytest.mark.parametrize(
    "phase, pending", [("mid_turn", True), ("post_turn", True), ("mid_turn", False)]
)
def test_finalizing_covered_assistant_fences_only_mid_turn_checkpoint(
    db_session, monkeypatch, phase, pending
):
    from core.chat import inflight
    from core.chat.compaction.service import _load_history

    chat = "covered-growth"
    db_session.add(ChatSession(chat_id=chat, user_id="u1", title="test"))
    db_session.commit()
    svc = ChatService(db_session)
    svc.message_repo.create(
        {
            "message_id": "growth-user",
            "chat_id": chat,
            "role": "user",
            "content": "original question",
        }
    )
    answer = svc.message_repo.create(
        {
            "message_id": "growth-answer",
            "chat_id": chat,
            "role": "assistant",
            "content": "before",
            "extra_data": {inflight.IN_FLIGHT_KEY: inflight.mark("growth-run", "running")},
        }
    )
    checkpoint = svc.add_compaction_checkpoint(
        chat,
        summary_text="old summary",
        replacement_history=[{"role": "user", "content": "old summary"}],
        covered_seq=answer.chat_seq,
        replacement_manifest={"phase": phase},
    )
    stale = svc.acquire_compaction_snapshot(chat, owner="stale", lease_seconds=60)
    assert stale is None  # The whole assistant row is covered by the checkpoint.
    svc.upsert_message(
        chat,
        "assistant",
        "after additional model step",
        message_id=answer.message_id,
        extra_data={
            "context_usage": {
                **USAGE,
                "captured_at_ms": int(checkpoint.created_at.timestamp() * 1000) + 1,
            }
        },
        commit=False,
        invalidate_mid_turn_checkpoint=pending,
    )
    db_session.commit()
    current = svc.get_latest_compaction_checkpoint(chat)
    if phase == "post_turn" or not pending:
        assert current.message_id == checkpoint.message_id
        return
    assert current is None
    history = _load_history(svc, chat, repair=False)
    assert "after additional model step" in str(history)
    monkeypatch.setattr(engine_mod, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    cfg = dataclasses.replace(
        config_settings.settings.compaction, enabled=True, keep_recent_tokens=1
    )
    monkeypatch.setattr(
        config_settings, "settings", dataclasses.replace(config_settings.settings, compaction=cfg)
    )

    async def summarize(history, *, timeout):
        return "complete summary"

    monkeypatch.setattr('core.chat.compaction.summary._summarize', summarize)
    assert asyncio.run(CompactionCoordinator(chat).compact(budget_inputs=INPUTS, token_limit=29520))
    db_session.expire_all()
    final = svc.get_latest_compaction_checkpoint(chat)
    assert final.extra_data["replacement_manifest"]["trigger_active_tokens"] == 40000
    assert final.extra_data["replacement_manifest"]["trigger_source"] == "provider"
