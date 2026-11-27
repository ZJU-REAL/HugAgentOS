"""Task evaluation: verdict storage, the reminder window and the pending list behind the reminder."""

from datetime import datetime, timedelta, timezone

import pytest
from core.auth.backend import UserContext, get_current_user
from core.chat import evaluation
from core.db.engine import Base, get_db
from core.db.models import ChatMessage, ChatRun, ChatSession, MessageFeedback, UserShadow
from core.evolution.evidence.outcomes import build_outcome
from core.infra.time import utc_now
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add_all([UserShadow(user_id="u1", username="u1"), UserShadow(user_id="u2", username="u2")])
    session.commit()
    yield session
    session.close()
    engine.dispose()


def _answer(db, key, *, user_id="u1", delivered=None, payload=None, channel_id=None, status="completed"):
    chat_id = f"chat_{key}"
    delivered = delivered or utc_now() - timedelta(minutes=5)
    db.add(ChatSession(chat_id=chat_id, user_id=user_id, title=f"标题{key}", channel_id=channel_id))
    db.add(ChatMessage(message_id=f"msg_{key}", chat_id=chat_id, role="assistant", chat_seq=2, content="答"))
    db.add(ChatRun(
        run_id=f"run_{key}", chat_id=chat_id, user_id=user_id, message_id=f"msg_{key}", status=status,
        request_payload=payload or {"mode_slug": "standard"}, created_at=delivered - timedelta(minutes=1),
        completed_at=delivered, updated_at=delivered,
    ))
    db.commit()


def _local(year, month, day, hour=10):
    return datetime(year, month, day, hour).astimezone().astimezone(timezone.utc)


def test_working_days_skip_weekends():
    friday, monday, wednesday = _local(2026, 11, 20), _local(2026, 11, 23), _local(2026, 11, 25)
    assert evaluation.working_days_between(friday, monday) == 1
    assert evaluation.working_days_between(friday, wednesday) == 3
    assert evaluation.working_days_between(monday, monday) == 0


def test_unrated_answers_turn_into_default_adopt_or_spot_check_after_three_working_days():
    delivered = _local(2026, 11, 20)  # Friday
    assert evaluation.unrated_state(delivered, complex_task=False, now=_local(2026, 11, 25)) == evaluation.PENDING
    assert evaluation.unrated_state(delivered, complex_task=False, now=_local(2026, 11, 26)) == evaluation.DEFAULT_ADOPT
    assert evaluation.unrated_state(delivered, complex_task=True, now=_local(2026, 11, 26)) == evaluation.SPOT_CHECK


@pytest.mark.parametrize("today", [_local(2026, 11, 23), _local(2026, 11, 26), _local(2026, 11, 28)])
def test_reminder_cutoff_matches_the_working_day_rule(today):
    cutoff = evaluation.reminder_cutoff(today)
    for hours_back in range(0, 24 * 10, 6):
        delivered = today - timedelta(hours=hours_back)
        pending = evaluation.unrated_state(delivered, complex_task=False, now=today) == evaluation.PENDING
        assert pending == (delivered >= cutoff), delivered


def test_legacy_ratings_map_onto_verdicts():
    assert evaluation.normalize_verdict("like") == evaluation.ADOPT
    assert evaluation.normalize_verdict("dislike") == evaluation.REJECT
    assert evaluation.effective_verdict("dislike", "revise") == evaluation.REVISE
    assert evaluation.normalize_verdict("meh") is None
    assert evaluation.clean_reasons(["efficiency", "x", "accuracy", "accuracy"]) == ["accuracy", "efficiency"]


def test_reasons_are_the_six_main_problems_and_retired_ones_still_read():
    assert list(evaluation.REASONS.values()) == [
        "模型回答不准确", "数据获取来源不准确", "数据统计计算错误", "数据口径出错", "模型误解用户意图", "模型回答效率低",
    ]
    # Retired options cannot be picked any more, but earlier records keep showing them.
    assert evaluation.clean_reasons(["completeness", "definition"]) == ["definition"]
    assert evaluation.stored_reasons(["completeness", "definition"]) == ["definition", "completeness"]


def test_revise_counts_as_a_milder_negative_signal():
    def feedback_value(rating):
        return next(s.value for s in build_outcome(feedback_rating=rating).signals if s.name == "explicit_feedback")

    assert feedback_value("adopt") == feedback_value("like") == 1.0
    assert feedback_value("reject") == -1.0
    assert -1.0 < feedback_value("revise") < 0


def test_pending_lists_recent_unrated_answers_of_own_ordinary_chats(db):
    now = utc_now()
    for i in range(7):
        _answer(db, f"new{i}", delivered=now - timedelta(minutes=10 + i))
    _answer(db, "rated")
    db.add(MessageFeedback(message_id="msg_rated", chat_id="chat_rated", user_id="u1", rating="like", verdict="adopt"))
    _answer(db, "old", delivered=now - timedelta(days=14))
    _answer(db, "other", user_id="u2")
    _answer(db, "im", channel_id="feishu_1")
    _answer(db, "auto", payload={"kind": "automation_prompt"})
    _answer(db, "scheduled", payload={"kind": "chat", "automation_task_id": "task_1"})
    _answer(db, "loop", payload={"kind": "chat", "loop_id": "loop_1"})
    _answer(db, "autoloop", payload={"kind": "autonomous_loop"})
    _answer(db, "failed", status="failed")
    db.commit()

    data = evaluation.pending_evaluations(db, "u1")

    assert [i["chat_id"] for i in data["items"]] == [f"chat_new{i}" for i in range(5)]
    assert set(data["chat_ids"]) == {f"chat_new{i}" for i in range(7)}
    assert data["total"] == 7
    assert data["items"][0]["message_id"] == "msg_new0"


def _client(db, monkeypatch, *, allowed=True, user_id="u1"):
    from api.middleware.error_handler import setup_error_handlers
    from api.routes.v1 import chats

    monkeypatch.setattr(
        "core.auth.capabilities.resolve_user_capabilities", lambda *_: {"can_use_evaluation": allowed}
    )
    app = FastAPI()
    setup_error_handlers(app)
    app.include_router(chats.router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id=user_id, user_center_id=user_id, username=user_id
    )
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_feedback_route_stores_verdict_reasons_and_legacy_rating(db, monkeypatch):
    _answer(db, "a")
    client = _client(db, monkeypatch)

    resp = client.post("/v1/chats/messages/msg_a/feedback", json={
        "rating": "revise", "reasons": ["intent", "definition", "completeness", "bogus"], "comment": "  缺少依据  ",
    })
    assert resp.status_code == 200, resp.text
    saved = resp.json()
    # The response is the stored record, so the client shows exactly what was kept.
    assert (saved["verdict"], saved["reasons"], saved["comment"]) == ("revise", ["definition", "intent"], "缺少依据")
    row = db.query(MessageFeedback).one()
    assert (row.rating, row.verdict, row.reasons, row.comment, row.chat_id) == (
        "dislike", "revise", ["definition", "intent"], "缺少依据", "chat_a")

    # Re-rating overwrites; adopt drops reasons.
    client.post("/v1/chats/messages/msg_a/feedback", json={"rating": "adopt", "reasons": ["accuracy"]})
    db.expire_all()
    row = db.query(MessageFeedback).one()
    assert (row.rating, row.verdict, row.reasons) == ("like", "adopt", [])

    items = client.get("/v1/chats/chat_a/feedback").json()["data"]["items"]
    assert items["msg_a"]["verdict"] == "adopt"

    assert client.post("/v1/chats/messages/msg_a/feedback", json={"rating": "meh"}).status_code == 400
    assert client.post("/v1/chats/messages/nope/feedback", json={"rating": "adopt"}).status_code == 404


def test_pending_route_requires_the_evaluation_permission(db, monkeypatch):
    _answer(db, "a")
    assert _client(db, monkeypatch, allowed=False).get("/v1/chats/evaluations/pending").status_code == 403
    data = _client(db, monkeypatch).get("/v1/chats/evaluations/pending").json()["data"]
    assert data["chat_ids"] == ["chat_a"]


def test_pending_route_reports_the_evaluation_gate_switch(db, monkeypatch):
    # The frontend reads it at start-up so a brand-new chat's first answer is held for the gate too.
    from core.config.store import evaluation_gate_options as gate_options

    _answer(db, "a")
    client = _client(db, monkeypatch)
    assert client.get("/v1/chats/evaluations/pending").json()["data"]["gate_enabled"] is False
    monkeypatch.setattr(gate_options, "evaluation_gate_options",
                        lambda: gate_options.EvaluationGateOptions(enabled=True, timeout_ms=5000))
    assert client.get("/v1/chats/evaluations/pending").json()["data"]["gate_enabled"] is True


def test_chat_feedback_is_not_readable_by_strangers(db, monkeypatch):
    _answer(db, "a")
    assert _client(db, monkeypatch, user_id="u2").get("/v1/chats/chat_a/feedback").status_code == 404


def test_scheduled_and_loop_answers_have_no_evaluation(db, monkeypatch):
    assert evaluation.is_evaluable({"kind": "chat", "mode_slug": "standard"})
    assert evaluation.is_evaluable(None)
    for payload in ({"kind": "chat", "automation_task_id": "t1"}, {"kind": "autonomous_loop"},
                    {"kind": "chat", "loop_id": "l1"}, {"kind": "automation_prompt"}):
        assert not evaluation.is_evaluable(payload)

    _answer(db, "asked")
    _answer(db, "scheduled", payload={"kind": "chat", "automation_task_id": "task_1"})
    client = _client(db, monkeypatch)

    resp = client.post("/v1/chats/messages/msg_scheduled/feedback", json={"rating": "adopt"})
    assert resp.status_code == 400
    assert db.query(MessageFeedback).count() == 0
    assert client.post("/v1/chats/messages/msg_asked/feedback", json={"rating": "adopt"}).status_code == 200

    data = client.get("/v1/chats/chat_scheduled/feedback").json()["data"]
    assert data["excluded"] == ["msg_scheduled"]
    assert client.get("/v1/chats/chat_asked/feedback").json()["data"]["excluded"] == []
