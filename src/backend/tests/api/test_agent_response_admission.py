"""Route-level concurrency proof for POST /v1/agents/responses admission."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from threading import Barrier

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.schemas import ChatRequest
from api.routes.v1.agent_responses import agent_response
from core.auth.backend import UserContext
from core.db.engine import Base
from core.db.models import BatchPlan, ChatMessage, ChatRun, ChatSession
from core.services.chat_sequencer import ChatSequencer


def _route_database(tmp_path, name="route.db"):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False, "timeout": 60},
    )
    Base.metadata.create_all(
        engine,
        tables=[ChatSession.__table__, ChatMessage.__table__, ChatRun.__table__],
    )
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(ChatSession(chat_id="chat-1", user_id="user-1", title="test"))
        db.commit()
    return engine, Session


def _user():
    return UserContext(user_id="user-1", user_center_id="center-1", username="test")


def _patch_common_chat_route(monkeypatch, chats):
    class FakeUserService:
        def __init__(self, _db):
            pass

        def get_user_settings(self, _user_id):
            return {}

    monkeypatch.setattr(chats, "UserService", FakeUserService)
    monkeypatch.setattr(chats, "_ensure_main_model_configured", lambda: None)
    monkeypatch.setattr(
        chats,
        "_resolve_chat_agent_targets",
        lambda _db, request, _user_id: (request, None, request.message, None),
    )
    monkeypatch.setattr(chats, "_resolve_selected_model_provider_id", lambda *_args: None)
    monkeypatch.setattr(
        chats, "_resolve_actual_chat_model_name", lambda request, _: request.model_name
    )
    monkeypatch.setattr(chats, "resolve_enabled_capabilities", lambda *_args: (None, None, None))
    monkeypatch.setattr(chats, "_ensure_chat_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chats, "_build_ctx", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(chats, "_build_user_extra_data", lambda *_args: {})


@pytest.mark.parametrize("transports", [(True, True), (False, False), (True, False)])
def test_concurrent_requests_have_one_durable_winner(monkeypatch, tmp_path, transports):
    import api.routes.v1.chats as chats
    from core.chat import plan_progress
    from orchestration import chat_run_executor

    engine = create_engine(
        f"sqlite:///{tmp_path / 'stream.db'}",
        connect_args={"check_same_thread": False, "timeout": 60},
    )
    Base.metadata.create_all(
        engine,
        tables=[ChatSession.__table__, ChatMessage.__table__, ChatRun.__table__],
    )
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add(ChatSession(chat_id="chat-1", user_id="user-1", title="test"))
        db.commit()

    class FakeUserService:
        def __init__(self, _db):
            pass

        def get_user_settings(self, _user_id):
            return {}

    async def fake_start_run(**kwargs):
        return kwargs["accepted_run"]

    async def empty_follow(_run_id, *, chat_id):
        if False:  # pragma: no cover - makes this an async generator
            yield chat_id

    monkeypatch.setattr(chats, "SessionLocal", Session)
    monkeypatch.setattr(chats, "UserService", FakeUserService)
    monkeypatch.setattr(chats, "_ensure_main_model_configured", lambda: None)
    monkeypatch.setattr(
        chats,
        "_resolve_chat_agent_targets",
        lambda _db, request, _user_id: (request, None, request.message, None),
    )
    monkeypatch.setattr(chats, "_resolve_selected_model_provider_id", lambda *_args: None)
    monkeypatch.setattr(
        chats, "_resolve_actual_chat_model_name", lambda request, _: request.model_name
    )
    monkeypatch.setattr(chats, "resolve_enabled_capabilities", lambda *_args: (None, None, None))
    monkeypatch.setattr(chats, "_ensure_chat_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chats, "_build_ctx", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(chats, "_build_user_extra_data", lambda *_args: {})
    monkeypatch.setattr(chats, "_load_session_messages", lambda *_args: [])
    monkeypatch.setattr(plan_progress, "clear_plan_progress", lambda _chat_id: None)
    monkeypatch.setattr(chat_run_executor, "start_run", fake_start_run)
    monkeypatch.setattr(chat_run_executor, "follow_run_as_sse", empty_follow)

    from api.routes.v1 import agent_responses
    from api.schemas import ChatResponse

    async def accepted_json(_run_id, *, chat_id, message_id):
        return ChatResponse(chat_id=chat_id, response="accepted", timestamp="now")

    monkeypatch.setattr(agent_responses, "_wait_response", accepted_json)
    barrier = Barrier(2)
    original_accept = ChatSequencer.accept_main_run

    def gated_accept(self, **kwargs):
        barrier.wait()
        return original_accept(self, **kwargs)

    monkeypatch.setattr(ChatSequencer, "accept_main_run", gated_accept)
    user = UserContext(
        user_id="user-1",
        user_center_id="center-1",
        username="test",
    )

    def send(label):
        with Session() as db:
            try:
                response = asyncio.run(
                    agent_response(
                        ChatRequest(chat_id="chat-1", message=label, stream=transports[label == "second"]),
                        user=user,
                        db=db,
                    )
                )
                return "accepted", response
            except HTTPException as exc:
                return "busy", exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(send, ("first", "second")))

    assert sorted(kind for kind, _result in outcomes) == ["accepted", "busy"]
    busy = next(result for kind, result in outcomes if kind == "busy")
    assert busy.status_code == 409
    assert busy.detail["code"] == "chat_busy"
    with Session() as db:
        message = db.query(ChatMessage).filter_by(role="user").one()
        reply = db.query(ChatMessage).filter_by(role="assistant").one()
        run = db.query(ChatRun).one()
        assert (reply.message_id, reply.content) == (run.message_id, "")
        assert busy.detail["active_run"] == {
            "run_id": run.run_id,
            "message_id": run.message_id,
            "status": "pending",
        }
        assert message.message_id == run.user_message_id
        assert (message.chat_seq, run.assistant_chat_seq) == (1, 2)
        assert db.get(ChatSession, "chat-1").next_message_seq == 3
    engine.dispose()


def test_non_stream_send_returns_busy_without_running_or_persisting(monkeypatch, tmp_path):
    import api.routes.v1.chats as chats

    engine, Session = _route_database(tmp_path, "send.db")
    with Session() as db:
        winner = ChatSequencer(db).accept_main_run(
            chat_id="chat-1",
            user_id="user-1",
            user_content="already running",
            request_payload={"kind": "stream"},
        )

    _patch_common_chat_route(monkeypatch, chats)
    monkeypatch.setattr(chats, "SessionLocal", Session)
    monkeypatch.setattr(chats, "_load_session_messages", lambda *_args: [])

    with Session() as db:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(
                agent_response(
                    ChatRequest(chat_id="chat-1", message="must be rejected"),
                    user=_user(),
                    db=db,
                )
            )

    assert raised.value.status_code == 409
    assert raised.value.detail["active_run"]["run_id"] == winner.run.run_id
    with Session() as db:
        assert [row.content for row in db.query(ChatMessage).order_by(ChatMessage.chat_seq)] == [
            "already running",
            "",
        ]
    engine.dispose()


def test_non_stream_send_uses_reserved_sequences_and_releases_writer(monkeypatch, tmp_path):
    import api.routes.v1.chats as chats
    from orchestration import chat_run_executor

    engine, Session = _route_database(tmp_path, "send-success.db")
    _patch_common_chat_route(monkeypatch, chats)
    monkeypatch.setattr(chats, "SessionLocal", Session)

    def load_after_accept(service, _chat_id, _user_id):
        stored = service.db.query(ChatMessage).filter_by(role="user").one()
        assert (stored.role, stored.chat_seq, stored.content) == ("user", 1, "hello")
        return [{"role": "user", "content": stored.content}]

    monkeypatch.setattr(chats, "_load_session_messages", load_after_accept)

    launched = {}

    async def fake_start_run(**kwargs):
        launched.update(kwargs)
        launched["run_id"] = kwargs["accepted_run"].run_id
        launched["message_id"] = kwargs["accepted_run"].message_id
        return kwargs["accepted_run"]

    async def fake_wait_run(_run_id):
        accepted = SimpleNamespace(run_id=launched["run_id"], message_id=launched["message_id"])
        with Session() as worker_db:
            # 助手行在接纳时已经存在；worker 只是把它定稿。
            reply = worker_db.get(ChatMessage, accepted.message_id)
            reply.content = "world"
            reply.extra_data = {
                "route": "main",
                "is_markdown": False,
                "sources": [],
                "artifacts": [],
                "warnings": [],
            }
            run = worker_db.get(ChatRun, accepted.run_id)
            run.status = "completed"
            run.writer_slot = None
            worker_db.commit()
        return SimpleNamespace(status="completed", error_message=None)

    monkeypatch.setattr(chat_run_executor, "start_run", fake_start_run)
    monkeypatch.setattr(chat_run_executor, "wait_run", fake_wait_run, raising=False)

    with Session() as db:
        response = asyncio.run(
            agent_response(ChatRequest(chat_id="chat-1", message="hello"), user=_user(), db=db)
        )

    assert response.response == "world"
    assert launched["raw_user_message"] == "hello"
    with Session() as db:
        assert [
            (row.role, row.chat_seq, row.content)
            for row in db.query(ChatMessage).order_by(ChatMessage.chat_seq)
        ] == [("user", 1, "hello"), ("assistant", 2, "world")]
        run = db.query(ChatRun).one()
        assert (run.status, run.writer_slot, run.user_chat_seq, run.assistant_chat_seq) == (
            "completed",
            None,
            1,
            2,
        )
    engine.dispose()




@pytest.mark.parametrize("stream", [False, True])
def test_both_transports_share_preferences_and_release_request_transaction(monkeypatch, tmp_path, stream):
    import api.routes.v1.chats as chats
    from api.routes.v1 import agent_responses
    from core.chat import plan_progress
    from orchestration import chat_run_executor

    engine, Session = _route_database(tmp_path, "preferences.db")
    _patch_common_chat_route(monkeypatch, chats)
    monkeypatch.setattr(chats, "SessionLocal", Session)
    monkeypatch.setattr(chats, "_load_session_messages", lambda *_args: [])
    monkeypatch.setattr(plan_progress, "clear_plan_progress", lambda _chat_id: None)

    async def preferences(_request, _user_id):
        return (["s"], ["a"], ["m"]), {
            "memory_enabled": True, "memory_write_enabled": True, "reranker_enabled": True,
        }

    captured = {}
    def context(*_args, **kwargs):
        captured.update(kwargs)
        return kwargs

    async def start(**kwargs):
        return kwargs["accepted_run"]

    monkeypatch.setattr(agent_responses, "_read_preferences", preferences)
    monkeypatch.setattr(chats, "_build_ctx", context)
    monkeypatch.setattr(chat_run_executor, "start_run", start)
    with Session() as db:
        result = asyncio.run(agent_responses._start_response_run(
            ChatRequest(chat_id="chat-1", message="hello", stream=stream), _user(), db
        ))
        assert not db.in_transaction()
        assert result.run_id
    assert captured["memory_enabled"] is True
    assert captured["memory_write_enabled"] is True
    assert captured["reranker_enabled"] is True
    engine.dispose()


@pytest.mark.parametrize("terminal,code", [
    ("cancelled", "run_cancelled"), ("needs_attention", "run_needs_attention"),
])
def test_json_wait_preserves_non_success_terminal_state(monkeypatch, terminal, code):
    from api.routes.v1 import agent_responses
    from orchestration import chat_run_executor

    record = SimpleNamespace(status=terminal, error_message=None)
    async def wait(_run_id):
        return record
    monkeypatch.setattr(chat_run_executor, "wait_run", wait)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(agent_responses._wait_response(
            "run-1", chat_id="chat-1", message_id="message-1"
        ))
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == code
    assert record.status == terminal
