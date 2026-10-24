"""Route-level concurrency proof for POST /v1/agents/responses admission."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import api.transports.run_sse as run_sse
import core.chat.context as chat_context
import core.chat.runtime.admission as runtime_admission
import core.db.engine as db_engine
import core.runs.runtime as runtime
import core.auth.users as chat_services_UserService_module
import core.chat.operations.admission as admission
import core.chat.operations.agent_targets as chat_agent_targets
import core.chat.operations.request_context as chat_request_context
import core.chat.operations.session_context as chat_session_context
import pytest
from api.routes.v1.agent_responses import agent_response
from core.auth.backend import UserContext
from core.chat.schemas import ChatRequest
from core.db.engine import Base
from core.db.models import ChatMessage, ChatRun, ChatSession
from core.chat.sequencer import ChatSequencer
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


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


def _patch_common_chat_route(monkeypatch):
    class FakeUserService:
        def __init__(self, _db):
            pass

        def get_user_settings(self, _user_id):
            return {}

    monkeypatch.setattr(chat_services_UserService_module, "UserService", FakeUserService)
    monkeypatch.setattr(chat_session_context, 'ensure_main_model_configured', lambda: None)
    monkeypatch.setattr(
        chat_agent_targets,
        "_resolve_chat_agent_targets",
        lambda _db, request, _user_id: (request, None, request.message, None),
    )
    monkeypatch.setattr(
        chat_session_context, 'resolve_selected_model_provider_id', lambda *_args: None
    )
    monkeypatch.setattr(
        chat_session_context,
        'resolve_actual_chat_model_name',
        lambda request, _: request.model_name,
    )
    monkeypatch.setattr(
        chat_context, "resolve_enabled_capabilities", lambda *_args: (None, None, None)
    )
    monkeypatch.setattr(
        chat_session_context, "_ensure_chat_session", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(chat_request_context, 'build_ctx', lambda *_args, **_kwargs: {})
    monkeypatch.setattr(chat_request_context, "_build_user_extra_data", lambda *_args: {})


@pytest.mark.parametrize("transports", [(True, True), (False, False), (True, False)])
def test_concurrent_requests_have_one_durable_winner(monkeypatch, tmp_path, transports):
    from core.chat import plan_progress
    pass

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

    monkeypatch.setattr(db_engine, "SessionLocal", Session)
    monkeypatch.setattr(chat_services_UserService_module, "UserService", FakeUserService)
    monkeypatch.setattr(chat_session_context, 'ensure_main_model_configured', lambda: None)
    monkeypatch.setattr(
        chat_agent_targets,
        "_resolve_chat_agent_targets",
        lambda _db, request, _user_id: (request, None, request.message, None),
    )
    monkeypatch.setattr(
        chat_session_context, 'resolve_selected_model_provider_id', lambda *_args: None
    )
    monkeypatch.setattr(
        chat_session_context,
        'resolve_actual_chat_model_name',
        lambda request, _: request.model_name,
    )
    monkeypatch.setattr(
        chat_context, "resolve_enabled_capabilities", lambda *_args: (None, None, None)
    )
    monkeypatch.setattr(
        chat_session_context, "_ensure_chat_session", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(chat_request_context, 'build_ctx', lambda *_args, **_kwargs: {})
    monkeypatch.setattr(chat_request_context, "_build_user_extra_data", lambda *_args: {})
    monkeypatch.setattr(chat_session_context, 'load_session_messages', lambda *_args: [])
    monkeypatch.setattr(plan_progress, "clear_plan_progress", lambda _chat_id: None)
    monkeypatch.setattr(runtime_admission, "start_run", fake_start_run)
    monkeypatch.setattr(run_sse, "follow_run_as_sse", empty_follow)

    from api.routes.v1 import agent_responses
    from core.chat.schemas import ChatResponse

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
        try:
            response = asyncio.run(
                agent_response(
                    ChatRequest(
                        chat_id="chat-1", message=label, stream=transports[label == "second"]
                    ),
                    user=user,
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

    engine, Session = _route_database(tmp_path, "send.db")
    with Session() as db:
        winner = ChatSequencer(db).accept_main_run(
            chat_id="chat-1",
            user_id="user-1",
            user_content="already running",
            request_payload={"kind": "stream"},
        )

    _patch_common_chat_route(monkeypatch)
    monkeypatch.setattr(db_engine, "SessionLocal", Session)
    monkeypatch.setattr(chat_session_context, 'load_session_messages', lambda *_args: [])

    with Session() as db:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(
                agent_response(
                    ChatRequest(chat_id="chat-1", message="must be rejected"),
                    user=_user(),
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
    pass

    engine, Session = _route_database(tmp_path, "send-success.db")
    _patch_common_chat_route(monkeypatch)
    monkeypatch.setattr(db_engine, "SessionLocal", Session)

    def load_after_accept(service, _chat_id, _user_id):
        stored = service.db.query(ChatMessage).filter_by(role="user").one()
        assert (stored.role, stored.chat_seq, stored.content) == ("user", 1, "hello")
        return [{"role": "user", "content": stored.content}]

    monkeypatch.setattr(chat_session_context, 'load_session_messages', load_after_accept)

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

    monkeypatch.setattr(runtime_admission, "start_run", fake_start_run)
    monkeypatch.setattr(runtime, "wait_run", fake_wait_run, raising=False)

    with Session() as db:
        response = asyncio.run(
            agent_response(ChatRequest(chat_id="chat-1", message="hello"), user=_user())
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
def test_both_transports_share_preferences_and_release_request_transaction(
    monkeypatch, tmp_path, stream
):
    from api.routes.v1 import agent_responses
    from core.chat import plan_progress
    pass

    engine, Session = _route_database(tmp_path, "preferences.db")
    _patch_common_chat_route(monkeypatch)
    monkeypatch.setattr(db_engine, "SessionLocal", Session)
    monkeypatch.setattr(chat_session_context, 'load_session_messages', lambda *_args: [])
    monkeypatch.setattr(plan_progress, "clear_plan_progress", lambda _chat_id: None)

    async def preferences(_request, _user_id):
        return (["s"], ["a"], ["m"]), {
            "memory_enabled": True,
            "memory_write_enabled": True,
            "reranker_enabled": True,
        }

    captured = {}

    def context(*_args, **kwargs):
        captured.update(kwargs)
        return kwargs

    async def start(**kwargs):
        return kwargs["accepted_run"]

    monkeypatch.setattr('core.chat.operations.admission._read_preferences', preferences)
    monkeypatch.setattr(chat_request_context, 'build_ctx', context)
    monkeypatch.setattr(runtime_admission, "start_run", start)
    with Session() as db:
        result = asyncio.run(
            admission.start_response_run(
                ChatRequest(chat_id="chat-1", message="hello", stream=stream), _user()
            )
        )
        assert not db.in_transaction()
        assert result.run_id
        saved_user = db.get(ChatMessage, result.user_message_id)
        assert saved_user.role == "user"
        assert saved_user.chat_id == result.chat_id
        assert saved_user.message_id == db.get(ChatRun, result.run_id).user_message_id
    assert captured["memory_enabled"] is True
    assert captured["memory_write_enabled"] is True
    assert captured["reranker_enabled"] is True
    engine.dispose()


@pytest.mark.parametrize(
    "terminal,code",
    [
        ("cancelled", "run_cancelled"),
        ("needs_attention", "run_needs_attention"),
    ],
)
def test_json_wait_preserves_non_success_terminal_state(monkeypatch, terminal, code):
    from api.routes.v1 import agent_responses
    pass

    record = SimpleNamespace(status=terminal, error_message=None)

    async def wait(_run_id):
        return record

    monkeypatch.setattr(runtime, "wait_run", wait)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            agent_responses._wait_response("run-1", chat_id="chat-1", message_id="message-1")
        )
    assert raised.value.status_code == 409
    assert raised.value.detail["code"] == code
    assert record.status == terminal
