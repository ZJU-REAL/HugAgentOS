"""A slow ownership query must not stall unrelated requests on the event loop."""

import asyncio
import threading
import time
from types import SimpleNamespace

import api.routes.v1.chats.questions as chat_questions
import api.routes.v1.chats.session_context as chat_session_context
import core.services as chat_services
import pytest
from core.llm.tools import user_questions


@pytest.mark.asyncio
async def test_pending_query_does_not_block_event_loop(monkeypatch):
    entered = threading.Event()
    loop_thread = threading.get_ident()

    class Service:
        def __init__(self, db):
            pass

        def get_session(self, *args):
            assert threading.get_ident() != loop_thread
            entered.set()
            time.sleep(0.15)
            return object()

    monkeypatch.setattr(chat_services, "ChatService", Service)
    monkeypatch.setattr(chat_session_context, "_release_request_session", lambda db: None)

    async def pending(chat_id):
        return []

    monkeypatch.setattr(user_questions, "get_all_pending_shared", pending)
    request = asyncio.create_task(
        chat_questions.get_pending_user_questions("chat", SimpleNamespace(user_id="u"), object())
    )
    for _ in range(100):
        if entered.is_set():
            break
        await asyncio.sleep(0.001)
    assert entered.is_set()
    assert not request.done(), "the loop should run while the database is sleeping"
    assert (await request)["data"]["requests"] == []
