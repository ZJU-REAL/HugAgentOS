"""Route-level concurrency proof for POST /v1/agents/responses admission."""

from __future__ import annotations

import asyncio

import api.routes.v1.chats.reruns as chat_reruns
import api.transports.run_sse as run_sse
import core.chat.context as chat_context
import core.chat.runtime.admission as admission
import core.db.engine as db_engine
import core.infra.responses as responses
import core.auth.users as chat_services_UserService_module
import core.chat.operations.agent_targets as chat_agent_targets
import core.chat.operations.invocation as chat_invocation
import core.chat.operations.request_context as chat_request_context
import core.chat.operations.schemas as chat_models
import core.chat.operations.session_context as chat_session_context
import pytest
from core.auth.backend import UserContext
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


def test_regenerate_busy_does_not_delete_existing_history(monkeypatch, tmp_path):
    from core.chat.service import ChatService

    engine, Session = _route_database(tmp_path, "regenerate.db")
    with Session() as db:
        service = ChatService(db)
        service.add_message(chat_id="chat-1", role="user", content="question")
        service.add_message(chat_id="chat-1", role="assistant", content="answer")
        winner = ChatSequencer(db).accept_main_run(
            chat_id="chat-1",
            user_id="user-1",
            user_content="already running",
            request_payload={"kind": "stream"},
        )

    _patch_common_chat_route(monkeypatch)
    monkeypatch.setattr(db_engine, "SessionLocal", Session)
    with Session() as db:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(
                chat_reruns.regenerate_message(
                    "chat-1", chat_models.RegenerateRequest(message_index=1), user=_user()
                )
            )

    assert raised.value.status_code == 409
    assert raised.value.detail["active_run"]["run_id"] == winner.run.run_id
    with Session() as db:
        assert [row.content for row in db.query(ChatMessage).order_by(ChatMessage.chat_seq)] == [
            "question",
            "answer",
            "already running",
            "",
        ]
    engine.dispose()


def test_regenerate_admits_then_deletes_tail_and_launches_reserved_run(monkeypatch, tmp_path):
    from core.chat.service import ChatService
    pass

    engine, Session = _route_database(tmp_path, "regenerate-success.db")
    with Session() as db:
        service = ChatService(db)
        service.add_message(chat_id="chat-1", role="user", content="question")
        service.add_message(chat_id="chat-1", role="assistant", content="old answer")
        service.add_message(chat_id="chat-1", role="user", content="later")

    _patch_common_chat_route(monkeypatch)
    monkeypatch.setattr(db_engine, "SessionLocal", Session)
    launched = {}

    def load_after_admission(service, _chat_id, _user_id):
        assert [
            row.content for row in service.db.query(ChatMessage).order_by(ChatMessage.chat_seq)
        ] == ["question", ""]
        return [{"role": "user", "content": "question"}]

    async def fake_start_run(**kwargs):
        launched.update(kwargs)
        launched["accepted_assistant_chat_seq"] = kwargs["accepted_run"].assistant_chat_seq
        return kwargs["accepted_run"]

    async def empty_follow(_run_id, *, chat_id):
        if False:  # pragma: no cover
            yield chat_id

    monkeypatch.setattr(chat_session_context, 'load_session_messages', load_after_admission)
    monkeypatch.setattr(admission, "start_run", fake_start_run)
    monkeypatch.setattr(run_sse, "follow_run_as_sse", empty_follow)

    with Session() as db:
        asyncio.run(
            chat_reruns.regenerate_message(
                "chat-1", chat_models.RegenerateRequest(message_index=1), user=_user()
            )
        )

    assert launched["raw_user_message"] == "question"
    assert launched["accepted_assistant_chat_seq"] == 4
    with Session() as db:
        run = db.query(ChatRun).one()
        assert (run.status, run.writer_slot, run.user_chat_seq, run.assistant_chat_seq) == (
            "pending",
            "main",
            1,
            4,
        )
    engine.dispose()


def test_edit_busy_does_not_delete_existing_history(monkeypatch, tmp_path):
    from core.chat.service import ChatService

    engine, Session = _route_database(tmp_path, "edit.db")
    with Session() as db:
        service = ChatService(db)
        service.add_message(chat_id="chat-1", role="user", content="keep")
        service.add_message(chat_id="chat-1", role="assistant", content="kept reply")
        service.add_message(chat_id="chat-1", role="user", content="old wording")
        service.add_message(chat_id="chat-1", role="assistant", content="old reply")
        winner = ChatSequencer(db).accept_main_run(
            chat_id="chat-1",
            user_id="user-1",
            user_content="already running",
            request_payload={"kind": "stream"},
        )

    _patch_common_chat_route(monkeypatch)
    monkeypatch.setattr(db_engine, "SessionLocal", Session)
    with Session() as db:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(
                chat_reruns.edit_and_resend(
                    "chat-1",
                    chat_models.EditAndResendRequest(message_index=2, new_content="new wording"),
                    user=_user(),
                )
            )

    assert raised.value.status_code == 409
    assert raised.value.detail["active_run"]["run_id"] == winner.run.run_id
    with Session() as db:
        assert [row.content for row in db.query(ChatMessage).order_by(ChatMessage.chat_seq)] == [
            "keep",
            "kept reply",
            "old wording",
            "old reply",
            "already running",
            "",
        ]
    engine.dispose()


def test_edit_replays_original_turn_invocation(monkeypatch, tmp_path):
    from core.chat.service import ChatService
    pass

    engine, Session = _route_database(tmp_path, "edit-invocation.db")
    saved_extra = {
        "attachments": [{"name": "a.png", "mime_type": "image/png", "file_id": "file-1"}],
        "skill_id": "skill-1",
        "skill_name": "PPT 设计",
        "connector_id": "mcp-1",
        "connector_name": "内网检索",
        "plugin_id": "office@user-1",
        "plugin_name": "办公套件",
        "mention_agent_id": "agent-1",
        "mention_name": "研究员",
        "quoted_follow_up": {"text": "原文引用"},
    }
    with Session() as db:
        service = ChatService(db)
        service.add_message(
            chat_id="chat-1", role="user", content="old wording", extra_data=saved_extra
        )
        service.add_message(chat_id="chat-1", role="assistant", content="old reply")

    _patch_common_chat_route(monkeypatch)
    monkeypatch.setattr(db_engine, "SessionLocal", Session)
    captured = {}

    def resolve_invocation(_db, request, _user_id):
        request._resolved_mcp_ids = [request.connector_id] if request.connector_id else []
        return request

    monkeypatch.setattr(
        chat_invocation, 'resolve_explicit_capability_invocation', resolve_invocation
    )

    def capture_ctx(request, *_args, **_kwargs):
        captured["request"] = request
        return {}

    monkeypatch.setattr(chat_request_context, 'build_ctx', capture_ctx)
    monkeypatch.setattr(chat_session_context, 'load_session_messages', lambda *_args: [])
    monkeypatch.setattr(responses, "sse_response", lambda stream: stream)
    monkeypatch.setattr(run_sse, "follow_run_as_sse", lambda *_a, **_k: None)

    async def fake_start_run(**kwargs):
        captured["start_run"] = kwargs
        return kwargs["accepted_run"]

    monkeypatch.setattr(admission, "start_run", fake_start_run)

    with Session() as db:
        asyncio.run(
            chat_reruns.edit_and_resend(
                "chat-1",
                chat_models.EditAndResendRequest(message_index=0, new_content="new wording"),
                user=_user(),
            )
        )

    request = captured["request"]
    assert [item.file_id for item in request.attachments] == ["file-1"]
    assert (request.skill_id, request.skill_name) == ("skill-1", "PPT 设计")
    assert (request.connector_id, request.connector_name) == ("mcp-1", "内网检索")
    assert request._resolved_mcp_ids == ["mcp-1"]
    assert request.plugin_id == "office@user-1"
    assert request.plugin_name == "办公套件"
    assert (request.mention_agent_id, request.mention_name) == ("agent-1", "研究员")
    assert "原文引用" in captured["start_run"]["effective_user_message"]

    with Session() as db:
        edited = (
            db.query(ChatMessage)
            .filter(ChatMessage.role == "user")
            .order_by(ChatMessage.chat_seq)
            .all()[-1]
        )
    assert edited.content == "new wording"
    assert edited.extra_data["attachments"] == saved_extra["attachments"]
    assert edited.extra_data["skill_name"] == "PPT 设计"
    assert edited.extra_data["connector_name"] == "内网检索"
    assert edited.extra_data["plugin_name"] == "办公套件"
    assert edited.extra_data["mention_name"] == "研究员"
    engine.dispose()
