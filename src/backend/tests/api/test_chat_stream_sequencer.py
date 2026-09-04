"""Route-level concurrency proof for POST /v1/chats/stream admission."""

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


def test_two_concurrent_stream_requests_have_one_durable_winner(monkeypatch, tmp_path):
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
                    chats.chat_stream(
                        ChatRequest(chat_id="chat-1", message=label),
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
    monkeypatch.setattr(chats, "_load_session_messages", lambda *_args: [])

    with Session() as db:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(
                chats.chat_send(
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

    def load_after_accept(service, _chat_id, _user_id):
        stored = service.db.query(ChatMessage).filter_by(role="user").one()
        assert (stored.role, stored.chat_seq, stored.content) == ("user", 1, "hello")
        return [{"role": "user", "content": stored.content}]

    monkeypatch.setattr(chats, "_load_session_messages", load_after_accept)

    launched = {}

    async def fake_start_run(**kwargs):
        launched.update(kwargs)
        return kwargs["accepted_run"]

    async def fake_wait_run(_run_id):
        accepted = launched["accepted_run"]
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
            chats.chat_send(ChatRequest(chat_id="chat-1", message="hello"), user=_user(), db=db)
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


def test_regenerate_busy_does_not_delete_existing_history(monkeypatch, tmp_path):
    import api.routes.v1.chats as chats
    from core.services.chat_service import ChatService

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

    _patch_common_chat_route(monkeypatch, chats)
    with Session() as db:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(
                chats.regenerate_message(
                    "chat-1", chats.RegenerateRequest(message_index=1), user=_user(), db=db
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
    import api.routes.v1.chats as chats
    from core.services.chat_service import ChatService
    from orchestration import chat_run_executor

    engine, Session = _route_database(tmp_path, "regenerate-success.db")
    with Session() as db:
        service = ChatService(db)
        service.add_message(chat_id="chat-1", role="user", content="question")
        service.add_message(chat_id="chat-1", role="assistant", content="old answer")
        service.add_message(chat_id="chat-1", role="user", content="later")

    _patch_common_chat_route(monkeypatch, chats)
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

    monkeypatch.setattr(chats, "_load_session_messages", load_after_admission)
    monkeypatch.setattr(chat_run_executor, "start_run", fake_start_run)
    monkeypatch.setattr(chat_run_executor, "follow_run_as_sse", empty_follow)

    with Session() as db:
        asyncio.run(
            chats.regenerate_message(
                "chat-1", chats.RegenerateRequest(message_index=1), user=_user(), db=db
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
    import api.routes.v1.chats as chats
    from core.services.chat_service import ChatService

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

    _patch_common_chat_route(monkeypatch, chats)
    with Session() as db:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(
                chats.edit_and_resend(
                    "chat-1",
                    chats.EditAndResendRequest(message_index=2, new_content="new wording"),
                    user=_user(),
                    db=db,
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
    import api.routes.v1.chats as chats
    from core.services.chat_service import ChatService
    from orchestration import chat_run_executor

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

    _patch_common_chat_route(monkeypatch, chats)
    captured = {}

    def resolve_invocation(_db, request, _user_id):
        request._resolved_mcp_ids = [request.connector_id] if request.connector_id else []
        return request

    monkeypatch.setattr(chats, "_resolve_explicit_capability_invocation", resolve_invocation)

    def capture_ctx(request, *_args, **_kwargs):
        captured["request"] = request
        return {}

    monkeypatch.setattr(chats, "_build_ctx", capture_ctx)
    monkeypatch.setattr(chats, "_load_session_messages", lambda *_args: [])
    monkeypatch.setattr(chats, "_release_request_session", lambda _db: None)
    monkeypatch.setattr(chats, "sse_response", lambda stream: stream)
    monkeypatch.setattr(chat_run_executor, "follow_run_as_sse", lambda *_a, **_k: None)

    async def fake_start_run(**kwargs):
        captured["start_run"] = kwargs
        return kwargs["accepted_run"]

    monkeypatch.setattr(chat_run_executor, "start_run", fake_start_run)

    with Session() as db:
        asyncio.run(
            chats.edit_and_resend(
                "chat-1",
                chats.EditAndResendRequest(message_index=0, new_content="new wording"),
                user=_user(),
                db=db,
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


def test_batch_resume_busy_does_not_delete_triggering_turn(monkeypatch, tmp_path):
    import api.routes.v1.batch as batch
    import api.routes.v1.chats as chats
    import core.chat.context as chat_context
    import core.services as services
    from core.services.chat_service import ChatService

    engine, Session = _route_database(tmp_path, "batch-resume.db")
    Base.metadata.create_all(engine, tables=[BatchPlan.__table__])
    with Session() as db:
        service = ChatService(db)
        service.add_message(chat_id="chat-1", role="user", content="batch question")
        service.add_message(
            chat_id="chat-1",
            role="assistant",
            content="",
            tool_calls=[{"name": "batch_plan", "output": {"plan_id": "plan-1"}}],
        )
        db.add(
            BatchPlan(
                plan_id="plan-1",
                user_id="user-1",
                chat_id="chat-1",
                source_type="text_list",
                items=[],
                prompt_template="{item}",
                status="running",
            )
        )
        db.commit()
        winner = ChatSequencer(db).accept_main_run(
            chat_id="chat-1",
            user_id="user-1",
            user_content="already running",
            request_payload={"kind": "stream"},
        )

    _patch_common_chat_route(monkeypatch, chats)
    monkeypatch.setattr(
        chat_context, "resolve_enabled_capabilities", lambda *_args: (None, None, None)
    )
    monkeypatch.setattr(services, "UserService", chats.UserService)

    with Session() as db:
        with pytest.raises(HTTPException) as raised:
            asyncio.run(batch.cancel_and_resume("plan-1", user=_user(), db=db))

    assert raised.value.status_code == 409
    assert raised.value.detail["active_run"]["run_id"] == winner.run.run_id
    with Session() as db:
        assert [row.content for row in db.query(ChatMessage).order_by(ChatMessage.chat_seq)] == [
            "batch question",
            "",
            "already running",
            "",
        ]
    engine.dispose()
