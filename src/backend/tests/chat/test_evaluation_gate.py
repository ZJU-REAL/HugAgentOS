"""Evaluation gate: parsing and fallbacks, the feedback-route lists and the reminder filter.

The task monitor's "not needed" state is tested in tests/features/task_monitor (EE-only module).
"""

from datetime import timedelta

import pytest
from core.auth.backend import UserContext, get_current_user
from core.chat import evaluation, evaluation_gate as gate
from core.config.store import evaluation_gate_options as gate_options
from core.db.engine import Base, get_db
from core.db.models import ChatMessage, ChatRun, ChatSession, MessageFeedback, UserShadow
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
    session.add(UserShadow(user_id="u1", username="u1"))
    session.commit()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def gate_on(monkeypatch):
    def switch(enabled=True, timeout_ms=5000):
        monkeypatch.setattr(
            gate_options, "evaluation_gate_options",
            lambda: gate_options.EvaluationGateOptions(enabled=enabled, timeout_ms=timeout_ms),
        )
    switch()
    return switch


def _answer(db, key, *, chat_id="chat_a", seq=2, delivered=None, decided=None, payload=None,
            status="completed", content="答", pending=False):
    delivered = delivered or utc_now() - timedelta(minutes=5)
    if db.get(ChatSession, chat_id) is None:
        db.add(ChatSession(chat_id=chat_id, user_id="u1", title="标题"))
    extra = {gate.STORAGE_KEY: decided} if decided is not None else {}
    if pending:
        extra[gate.PENDING_KEY] = True
    db.add(ChatMessage(message_id=f"q_{key}", chat_id=chat_id, role="user", chat_seq=seq - 1,
                       content=f"问{key}"))
    db.add(ChatMessage(message_id=f"msg_{key}", chat_id=chat_id, role="assistant", chat_seq=seq,
                       content=content, extra_data=extra))
    db.add(ChatRun(
        run_id=f"run_{key}", chat_id=chat_id, user_id="u1", message_id=f"msg_{key}", status=status,
        request_payload=payload or {"mode_slug": "standard"},
        created_at=delivered - timedelta(minutes=1), completed_at=delivered, updated_at=delivered,
    ))
    db.commit()


NO = gate.result(False, "简单问答", source=gate.SOURCE_MODEL, cause=None, latency_ms=300)
YES = gate.result(True, "完整报告", source=gate.SOURCE_MODEL, cause=None, latency_ms=300)


# ── parsing and fallback ───────────────────────────────────────────────────


@pytest.mark.parametrize("text, need", [
    ('{"need_evaluation": false, "reason": "寒暄"}', False),
    ('<think>想想</think>```json\n{"need_evaluation": true, "reason": "交付了报告"}\n```', True),
    ('结论如下：{"need_evaluation": "否", "reason": "重复追问"} 以上', False),
    ('{"need_evaluation": "true"}', True),
])
def test_parse_is_lenient(text, need):
    assert gate.parse_decision(text)["need_evaluation"] is need


def test_reason_is_clipped_to_twenty_chars():
    decided = gate.resolve_outcome(gate.ModelOutcome(
        text='{"need_evaluation": false, "reason": "' + "很长的理由" * 10 + '"}', latency_ms=42))
    assert decided["need_evaluation"] is False and len(decided["reason"]) == gate.REASON_MAX
    assert (decided["source"], decided["cause"], decided["latency_ms"]) == ("model", None, 42)
    assert decided["decided_at"]


@pytest.mark.parametrize("outcome, cause", [
    (gate.ModelOutcome(error=gate.CAUSE_TIMEOUT), "timeout"),
    (gate.ModelOutcome(error=gate.CAUSE_MODEL_ERROR), "model_error"),
    (gate.ModelOutcome(error=gate.CAUSE_TOOL_CALL), "tool_call"),
    (gate.ModelOutcome(text="我觉得需要"), "parse_error"),
    (gate.ModelOutcome(text='{"need_evaluation": "maybe"}'), "parse_error"),
    (gate.ModelOutcome(text='["no"]'), "parse_error"),
])
def test_every_failure_falls_back_to_needing_evaluation(outcome, cause):
    decided = gate.resolve_outcome(outcome)
    assert decided["need_evaluation"] is True
    assert (decided["source"], decided["cause"]) == ("fallback", cause)


def test_options_read_errors_as_disabled(monkeypatch):
    from core.config.store import system_config

    class Broken:
        @staticmethod
        def get_instance():
            raise RuntimeError("config down")

    monkeypatch.setattr(system_config, "SystemConfigService", Broken)
    assert gate_options.evaluation_gate_options() == gate_options.DISABLED

    class Store:
        values = {"evaluation_gate.enable": "true", "evaluation_gate.timeout_ms": "10"}

        @staticmethod
        def get_instance():
            return Store()

        def get(self, key, default=None):
            return self.values.get(key, default)

    monkeypatch.setattr(system_config, "SystemConfigService", Store)
    assert gate_options.evaluation_gate_options() == gate_options.EvaluationGateOptions(True, 500)


def test_transcript_marks_rated_and_gated_answers_and_the_target(db):
    _answer(db, "1", seq=2, content="第一份报告")
    _answer(db, "2", seq=4, decided=NO)
    _answer(db, "3", seq=6, content="新的回答")
    db.add(MessageFeedback(message_id="msg_1", chat_id="chat_a", user_id="u1", rating="like",
                           verdict="adopt"))
    db.commit()
    entries = gate.load_transcript(db, "chat_a", "msg_3")
    assert [e.message_id for e in entries] == ["q_1", "msg_1", "q_2", "msg_2", "q_3", "msg_3"]
    notes = {e.message_id: e.note for e in entries}
    assert notes["msg_1"] == "用户已评价：满意" and notes["msg_2"] == "已判定：无需评价"
    blocks = gate.render_transcript(entries)
    assert blocks[-1].startswith("[第 3 轮 · 助手 · 最新这条回答（待判定）]")
    assert "新的回答" in blocks[-1]
    assert gate.load_transcript(db, "chat_a", "nope") == []


# ── GET /v1/chats/{chat_id}/feedback ───────────────────────────────────────


def _client(db, monkeypatch):
    from api.middleware.error_handler import setup_error_handlers
    from api.routes.v1 import chats

    monkeypatch.setattr(
        "core.auth.capabilities.resolve_user_capabilities", lambda *_: {"can_use_evaluation": True}
    )
    app = FastAPI()
    setup_error_handlers(app)
    app.include_router(chats.router)
    app.dependency_overrides[get_current_user] = lambda: UserContext(
        user_id="u1", user_center_id="u1", username="u1"
    )
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _seed_lists(db):
    now = utc_now()
    _answer(db, "no", seq=2, decided=NO)
    _answer(db, "yes", seq=4, decided=YES)
    _answer(db, "fresh", seq=6, delivered=now - timedelta(seconds=3), pending=True)
    _answer(db, "late", seq=8, delivered=now - timedelta(seconds=30), pending=True)
    _answer(db, "live", seq=10, delivered=now - timedelta(seconds=1), status="running",
            pending=True)
    # Completed without the marker (plan execution, snapshot recovery, manual compaction):
    # no gate was spawned, so it is never "gating".
    _answer(db, "plan", seq=14, delivered=now - timedelta(seconds=2))
    _answer(db, "sched", seq=12, decided=NO, delivered=now - timedelta(seconds=1),
            payload={"kind": "chat", "automation_task_id": "t1"})


def test_feedback_lists_when_the_gate_is_on(db, monkeypatch, gate_on):
    _seed_lists(db)
    data = _client(db, monkeypatch).get("/v1/chats/chat_a/feedback").json()["data"]
    assert data["gate_enabled"] is True
    assert data["not_needed"] == ["msg_no"]
    # Delivered within timeout + 10s, no result yet; the 30s-old one counts as needing evaluation.
    assert data["gating"] == ["msg_fresh"]
    assert data["excluded"] == ["msg_sched"]
    assert data["items"] == {}

    gate_on(timeout_ms=30_000)
    data = _client(db, monkeypatch).get("/v1/chats/chat_a/feedback").json()["data"]
    assert data["gating"] == ["msg_fresh", "msg_late"]


def test_feedback_lists_are_empty_when_the_gate_is_off(db, monkeypatch, gate_on):
    _seed_lists(db)
    gate_on(enabled=False)
    data = _client(db, monkeypatch).get("/v1/chats/chat_a/feedback").json()["data"]
    assert data["not_needed"] == [] and data["gating"] == []
    assert data["gate_enabled"] is False
    assert data["excluded"] == ["msg_sched"]


def test_not_needed_answers_can_still_be_rated(db, monkeypatch, gate_on):
    _answer(db, "no", decided=NO)
    resp = _client(db, monkeypatch).post("/v1/chats/messages/msg_no/feedback",
                                         json={"rating": "adopt"})
    assert resp.status_code == 200, resp.text


def test_rerun_reads_the_latest_run_of_a_reused_message(db, gate_on):
    _answer(db, "a", delivered=utc_now() - timedelta(minutes=5), pending=True)
    # A rerun answered the same message again just now; its gate is still being decided.
    db.add(ChatRun(run_id="run_a2", chat_id="chat_a", user_id="u1", message_id="msg_a",
                   status="completed", request_payload={"mode_slug": "standard"},
                   created_at=utc_now() - timedelta(seconds=4), completed_at=utc_now(),
                   updated_at=utc_now()))
    db.commit()
    lists = gate.chat_gate_lists(db, "chat_a", enabled=True, timeout_ms=5000)
    assert lists == {"not_needed": [], "gating": ["msg_a"]}
    assert gate.latest_run_is(db, "msg_a", "run_a2") and not gate.latest_run_is(db, "msg_a", "run_a")


def test_gate_lists_never_repeat_excluded_answers(db, gate_on):
    _answer(db, "no", decided=NO)
    _answer(db, "wait", seq=4, delivered=utc_now(), pending=True)
    assert gate.chat_gate_lists(db, "chat_a", enabled=True, timeout_ms=5000) == {
        "not_needed": ["msg_no"], "gating": ["msg_wait"]}
    lists = gate.chat_gate_lists(db, "chat_a", enabled=True, timeout_ms=5000,
                                 excluded=["msg_no", "msg_wait"])
    assert lists == {"not_needed": [], "gating": []}


# ── reminders ──────────────────────────────────────────────────────────────


def test_pending_skips_not_needed_answers_only_when_on(db, gate_on):
    _answer(db, "no", chat_id="chat_no", decided=NO)
    _answer(db, "yes", chat_id="chat_yes", decided=YES)
    _answer(db, "none", chat_id="chat_none")
    # Still being decided: not reminded about yet (it may turn out not to need a rating) …
    _answer(db, "deciding", chat_id="chat_deciding", pending=True,
            delivered=utc_now() - timedelta(seconds=2))
    # … but once the wait is over without a result it reads as needing evaluation.
    _answer(db, "lost", chat_id="chat_lost", pending=True,
            delivered=utc_now() - timedelta(minutes=5))
    data = evaluation.pending_evaluations(db, "u1")
    assert set(data["chat_ids"]) == {"chat_yes", "chat_none", "chat_lost"} and data["total"] == 3

    gate_on(enabled=False)
    data = evaluation.pending_evaluations(db, "u1")
    assert set(data["chat_ids"]) == {"chat_no", "chat_yes", "chat_none", "chat_deciding",
                                     "chat_lost"}
    assert data["total"] == 5


def test_gate_expected_is_the_switch_and_evaluability(db, gate_on):
    _answer(db, "asked")
    _answer(db, "sched", seq=4, payload={"kind": "chat", "automation_task_id": "t1"})
    assert gate.gate_expected(db, "run_asked") is True
    assert gate.gate_expected(db, "run_sched") is False
    assert gate.gate_expected(db, "nope") is False
    gate_on(enabled=False)
    assert gate.gate_expected(db, "run_asked") is False


def test_extra_data_merge_keeps_a_concurrent_writers_key(tmp_path):
    """Follow-ups and the gate patch one message right after completion; neither may drop the other."""
    from core.chat.service import ChatService

    engine = create_engine(f"sqlite:///{tmp_path / 'merge.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as setup:
        setup.add(UserShadow(user_id="u1", username="u1"))
        setup.add(ChatSession(chat_id="c", user_id="u1", title="t"))
        setup.add(ChatMessage(message_id="m", chat_id="c", role="assistant", chat_seq=1,
                              content="答", extra_data={"route": "main"}))
        setup.commit()
    first, second = Session(), Session()
    try:
        # Both writers loaded the message before either wrote (the lost-update interleaving).
        assert first.get(ChatMessage, "m").extra_data == {"route": "main"}
        assert second.get(ChatMessage, "m").extra_data == {"route": "main"}
        assert ChatService(first).update_message_extra_data("m", {"follow_up_questions": ["q?"]})
        assert ChatService(second).update_message_extra_data("m", {gate.STORAGE_KEY: NO})
        assert not ChatService(second).update_message_extra_data("missing", {"x": 1})
    finally:
        first.close()
        second.close()
    with Session() as check:
        extra = check.get(ChatMessage, "m").extra_data
    assert extra == {"route": "main", "follow_up_questions": ["q?"], gate.STORAGE_KEY: NO}
    engine.dispose()
