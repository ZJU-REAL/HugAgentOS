"""HTTP seam tests for ``POST /v1/chats/{chat_id}/compact``."""

from __future__ import annotations

import dataclasses

import core.db.engine as engine_mod
import httpx
import pytest
from api.middleware.error_handler import setup_error_handlers
from api.routes.v1 import chats
from core.auth.backend import UserContext, get_current_user
from core.chat.compaction import service as S
from core.chat.service import ChatService
from core.config import settings as config_settings
from core.db.models import ChatRun, ChatSession
from core.llm.context import compaction as C
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

CHAT_ID = "chat_manual_compact"


@pytest.fixture
def svc(db_session, monkeypatch):
    monkeypatch.setattr(engine_mod, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    db_session.add(ChatSession(chat_id=CHAT_ID, user_id="owner", title="manual"))
    db_session.commit()
    return ChatService(db_session)


def _keep_recent(monkeypatch, tokens: int, **overrides) -> None:
    real = config_settings.settings
    compaction = dataclasses.replace(real.compaction, keep_recent_tokens=tokens, **overrides)
    monkeypatch.setattr(
        config_settings, "settings", dataclasses.replace(real, compaction=compaction)
    )


def _message(svc: ChatService, message_id: str, role: str, content: str) -> None:
    svc.message_repo.create(
        {"message_id": message_id, "chat_id": CHAT_ID, "role": role, "content": content}
    )


def _conversation(svc: ChatService) -> None:
    _message(svc, "m1", "user", "first question " * 40)
    _message(svc, "m2", "assistant", "first answer " * 40)
    _message(svc, "m3", "user", "second question")
    _message(svc, "m4", "assistant", "second answer")


def _summary(monkeypatch, text: str = "the summary"):
    calls = []

    async def fake_summarize(history, *, timeout):
        calls.append(history)
        return text

    monkeypatch.setattr("core.chat.compaction.summary._summarize", fake_summarize)
    return calls


async def _post(user_id: str = "owner", chat_id: str = CHAT_ID) -> httpx.Response:
    app = FastAPI()
    setup_error_handlers(app)
    app.include_router(chats.router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id, user_center_id=user_id, username=user_id
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(f"/v1/chats/{chat_id}/compact")


@pytest.mark.asyncio
async def test_compact_publishes_checkpoint_used_by_next_turn(svc, monkeypatch):
    _conversation(svc)
    _keep_recent(monkeypatch, 10)
    calls = _summary(monkeypatch)

    response = await _post()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["compacted"] is True
    assert data["tokens_before"] > data["tokens_after"] > 0
    assert len(calls) == 1

    svc.db.expire_all()
    checkpoint = svc.get_latest_compaction_checkpoint(CHAT_ID)
    assert checkpoint is not None
    assert checkpoint.extra_data["notice_pending"] is True
    # The next turn loads the replacement history, not the raw rows.
    history = S._load_history(svc, CHAT_ID, repair=False)
    assert C.is_summary_message(str(history[0]["content"]))
    assert "the summary" in str(history[0]["content"])
    assert "second answer" in str(history[-1]["content"])
    assert not any("first question" in str(item["content"]) for item in history)

    # Compacting again right away finds nothing newer than the checkpoint.
    again = await _post()
    assert again.status_code == 200, again.text
    assert again.json()["data"]["compacted"] is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_compact_reports_nothing_when_history_fits_the_kept_tail(svc, monkeypatch):
    _conversation(svc)
    _keep_recent(monkeypatch, 1_000_000)
    calls = _summary(monkeypatch)

    response = await _post()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["compacted"] is False
    assert data["tokens_after"] is None
    assert calls == []
    svc.db.expire_all()
    assert svc.get_latest_compaction_checkpoint(CHAT_ID) is None


@pytest.mark.asyncio
async def test_compact_empty_chat_is_nothing_to_compact(svc, monkeypatch):
    calls = _summary(monkeypatch)

    response = await _post()

    assert response.status_code == 200, response.text
    assert response.json()["data"] == {
        "compacted": False,
        "tokens_before": None,
        "tokens_after": None,
    }
    assert calls == []


@pytest.mark.asyncio
async def test_compact_conflicts_with_an_active_run(svc, monkeypatch):
    _conversation(svc)
    _keep_recent(monkeypatch, 10)
    calls = _summary(monkeypatch)
    svc.db.add(
        ChatRun(
            run_id="run_live",
            chat_id=CHAT_ID,
            user_id="owner",
            message_id="m_live",
            status="running",
        )
    )
    svc.db.commit()

    response = await _post()

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "chat_busy"
    assert detail["active_run"]["run_id"] == "run_live"
    assert calls == []
    assert svc.get_latest_compaction_checkpoint(CHAT_ID) is None


@pytest.mark.asyncio
async def test_finished_run_does_not_block_compaction(svc, monkeypatch):
    _conversation(svc)
    _keep_recent(monkeypatch, 10)
    _summary(monkeypatch)
    svc.db.add(
        ChatRun(
            run_id="run_done",
            chat_id=CHAT_ID,
            user_id="owner",
            message_id="m4",
            status="completed",
        )
    )
    svc.db.commit()

    response = await _post()

    assert response.status_code == 200, response.text
    assert response.json()["data"]["compacted"] is True


@pytest.mark.asyncio
async def test_other_users_chat_is_not_found(svc, monkeypatch):
    _conversation(svc)
    _keep_recent(monkeypatch, 10)
    calls = _summary(monkeypatch)

    response = await _post(user_id="intruder")
    missing = await _post(chat_id="chat_does_not_exist")

    assert response.status_code == 404
    assert missing.status_code == 404
    assert calls == []
    assert svc.get_latest_compaction_checkpoint(CHAT_ID) is None


@pytest.mark.asyncio
async def test_compact_conflicts_with_a_running_compactor(svc, monkeypatch):
    from datetime import timedelta

    from core.infra.time import utc_now

    _conversation(svc)
    _keep_recent(monkeypatch, 10)
    calls = _summary(monkeypatch)
    state = svc._ensure_compaction_state(CHAT_ID)
    state.lease_owner = "compactor:other"
    state.lease_expires_at = utc_now() + timedelta(minutes=5)
    svc.db.commit()

    response = await _post()

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "compaction_busy"
    assert calls == []


@pytest.mark.asyncio
async def test_summary_failure_is_an_error_not_nothing_to_compact(svc, monkeypatch):
    _conversation(svc)
    _keep_recent(monkeypatch, 10)
    _summary(monkeypatch, text="")

    response = await _post()

    assert response.status_code == 503
    svc.db.expire_all()
    assert svc.get_latest_compaction_checkpoint(CHAT_ID) is None
