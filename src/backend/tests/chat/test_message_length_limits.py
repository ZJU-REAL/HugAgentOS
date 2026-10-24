"""User-message length limits: 100000 characters end to end."""

from __future__ import annotations

import pytest
from core.chat.limits import CONTENT_MAX
from core.chat.operations.schemas import EditAndResendRequest, SteerChatRunRequest
from core.chat.schemas import ChatRequest
from core.chat.service import ChatService
from core.db.models import ChatMessage, ChatSession
from pydantic import ValidationError

LIMIT = 100_000


def test_limit_matches_the_stored_content_column():
    assert CONTENT_MAX == LIMIT


def test_chat_request_accepts_100000_and_rejects_100001():
    assert len(ChatRequest(chat_id="c", message="x" * LIMIT).message) == LIMIT
    with pytest.raises(ValidationError):
        ChatRequest(chat_id="c", message="x" * (LIMIT + 1))


def test_steer_request_stays_within_the_steer_queue_limit():
    # chat_steer_queue.message keeps its 10000-character CHECK; the request must
    # reject what the queue would refuse instead of failing inside it.
    assert len(SteerChatRunRequest(steer_id="s", message="x" * 10_000).message) == 10_000
    with pytest.raises(ValidationError):
        SteerChatRunRequest(steer_id="s", message="x" * 10_001)


def test_edit_and_resend_accepts_what_send_accepts():
    body = EditAndResendRequest(message_index=0, new_content="x" * LIMIT)
    assert len(body.new_content) == LIMIT
    with pytest.raises(ValidationError):
        EditAndResendRequest(message_index=0, new_content="x" * (LIMIT + 1))


def test_longest_accepted_message_is_stored_verbatim(db_session):
    db_session.add(ChatSession(chat_id="chat_long", user_id="u1", title="long"))
    db_session.commit()
    line = "def f():\n    return 1\n"
    pasted = line * (LIMIT // len(line)) + "#" * (LIMIT % len(line))
    assert len(pasted) == LIMIT

    ChatService(db_session).add_message("chat_long", "user", pasted, message_id="m_long")

    db_session.expire_all()
    stored = db_session.get(ChatMessage, "m_long").content
    assert stored == pasted
