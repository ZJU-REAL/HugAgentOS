"""HTTP retries remain possible when the admitted worker fails to start."""

import pytest
from api.routes.v1 import chats
from core.auth.backend import get_current_user
from core.db.engine import Base, get_db
from core.services.chat_service import ChatService
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.api.test_chat_stream_sequencer import _patch_common_chat_route, _route_database, _user


@pytest.mark.parametrize(
    "route,body",
    [
        ("regenerate", {"message_index": 1}),
        ("edit", {"message_index": 0, "new_content": "revised question"}),
    ],
)
def test_worker_start_failure_releases_admission_for_http_retry(monkeypatch, tmp_path, route, body):
    from core.db import engine as db_engine
    from orchestration import chat_run_executor

    engine, Session = _route_database(tmp_path)
    Base.metadata.create_all(engine)
    with Session() as db:
        service = ChatService(db)
        service.add_message(chat_id="chat-1", role="user", content="question")
        service.add_message(chat_id="chat-1", role="assistant", content="old answer")
    _patch_common_chat_route(monkeypatch)
    monkeypatch.setattr(db_engine, "SessionLocal", Session)
    calls = 0

    async def start(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected worker startup failure")
        return kwargs["accepted_run"]

    async def follow(*args, **kwargs):
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_run_executor, "start_run", start)
    monkeypatch.setattr(chat_run_executor, "follow_run_as_sse", follow)
    app = FastAPI()
    app.include_router(chats.router)

    @app.exception_handler(Exception)
    async def report_test_error(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=500, content={"error": str(exc)})

    def database():
        with Session() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = _user
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            url = f"/v1/chats/chat-1/{route}"
            failed = client.post(url, json=body)
            assert failed.status_code == 500
            assert "injected worker startup failure" in failed.text, failed.text
            retry = client.post(url, json=body)
            assert retry.status_code == 200, retry.text
            assert "[DONE]" in retry.text
            assert calls == 2
    finally:
        engine.dispose()
