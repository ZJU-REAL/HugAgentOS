"""Reruns preserve current model and prompt surface at real admission boundaries."""

import asyncio

import api.transports.run_sse as run_sse
import core.chat.runtime.admission as admission
import pytest
from core.db.engine import Base
from core.db.models import ChatRun
from core.chat.service import ChatService
from tests.api.test_chat_stream_sequencer import _patch_common_chat_route, _route_database, _user


@pytest.mark.parametrize("operation", ["edit", "regenerate"])
@pytest.mark.parametrize("override", [False, True])
def test_rerun_model_and_mode_survive_admission(monkeypatch, tmp_path, operation, override):
    from api.routes.v1.chats import reruns
    from core.db import engine as db_engine
    from core.chat.operations import schemas as models
    from core.chat.operations import session_context
    pass

    engine, Session = _route_database(tmp_path)
    Base.metadata.create_all(engine)
    saved = {"model_provider_id": "old-provider", "mode_slug": "turbo", "chat_mode": "fast"}
    expected = (
        {"model_provider_id": "selected-provider", "mode_slug": "standard", "chat_mode": "high"}
        if override
        else saved
    )
    with Session() as db:
        svc = ChatService(db)
        svc.add_message(chat_id="chat-1", role="user", content="question", extra_data=saved)
        svc.add_message(chat_id="chat-1", role="assistant", content="old answer")
    _patch_common_chat_route(monkeypatch)
    monkeypatch.setattr(db_engine, "SessionLocal", Session)
    monkeypatch.setattr(
        session_context,
        'resolve_selected_model_provider_id',
        lambda db, req, uid: req.model_provider_id,
    )

    async def start(**kwargs):
        return kwargs["accepted_run"]

    async def follow(*args, **kwargs):
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(admission, "start_run", start)
    monkeypatch.setattr(run_sse, "follow_run_as_sse", follow)
    options = expected if override else {}
    with Session() as db:
        if operation == "edit":
            asyncio.run(
                reruns.edit_and_resend(
                    "chat-1",
                    models.EditAndResendRequest(message_index=0, new_content="short", **options),
                    user=_user(),
                )
            )
        else:
            asyncio.run(
                reruns.regenerate_message(
                    "chat-1",
                    models.RegenerateRequest(message_index=1, **options),
                    user=_user(),
                )
            )
    with Session() as db:
        payload = db.query(ChatRun).one().request_payload
        assert {key: payload.get(key) for key in expected} == expected
        row = ChatService(db).get_message_by_index("chat-1", 0)
        assert {key: row.extra_data.get(key) for key in expected} == expected
    if operation == "regenerate" and override:
        with Session() as db:
            run = db.query(ChatRun).one()
            run.status = "completed"
            run.writer_slot = None
            db.commit()
        with Session() as db:
            asyncio.run(
                reruns.regenerate_message(
                    "chat-1", models.RegenerateRequest(message_index=1), user=_user()
                )
            )
        with Session() as db:
            payload = (
                db.query(ChatRun)
                .order_by(ChatRun.assistant_chat_seq.desc())
                .first()
                .request_payload
            )
            assert {key: payload.get(key) for key in expected} == expected
    engine.dispose()
