"""Chats the run journal creates for batch-job sub-agents stay out of every listing.

Each sub-agent call of a batch job binds a durable run to an ``internal_…`` chat with
no conversation in it. Listed, they flooded the user's history with empty
"internal_job_agent run" entries; they must stay reachable by id, because the job's
own runtime reads its chat (approval policy, history) like any other.
"""

from core.chat.service import ChatService
from core.db.models import ChatSession, UserShadow
from core.db.repository.chat import ChatSessionRepository
from core.db.reserved_chats import BACKGROUND_CHAT_PREFIX

OWNER = "background-owner"


def test_background_run_chats_are_listed_nowhere_but_stay_reachable(db_session):
    db_session.add(UserShadow(user_id=OWNER, username="Owner"))
    internal_id = f"{BACKGROUND_CHAT_PREFIX}abc"
    db_session.add_all(
        [
            ChatSession(chat_id=internal_id, user_id=OWNER, title="internal_job_agent run"),
            ChatSession(chat_id="chat_visible", user_id=OWNER, title="Hello"),
        ]
    )
    db_session.commit()

    sessions, total = ChatSessionRepository(db_session).list_by_user(OWNER)

    assert [s.chat_id for s in sessions] == ["chat_visible"] and total == 1
    assert ChatService(db_session).get_session(internal_id, OWNER) is not None
