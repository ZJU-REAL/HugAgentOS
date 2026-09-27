"""Fork contract through HTTP and the normal history replay boundary."""

from uuid import uuid4

import pytest
from core.auth.backend import UserContext, get_current_user
from core.db.engine import Base, get_db
from core.db.models import ChatCompactionState, ChatMessage, ChatRun, ChatSession, Project
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def fork_env(tmp_path):
    from api.routes.v1.chat_forks import router
    from api.routes.v1.chats import router as chats_router

    engine = create_engine(
        f"sqlite:///{tmp_path / 'fork.db'}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(
        engine,
        tables=[
            ChatSession.__table__,
            ChatMessage.__table__,
            ChatRun.__table__,
            ChatCompactionState.__table__,
            Project.__table__,
        ],
    )
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        db.add(
            ChatSession(
                chat_id="source",
                user_id="owner",
                title="Original",
                extra_data={"agent_id": "a1", "plan_progress": {"id": "plan"}},
                next_message_seq=5,
            )
        )
        db.flush()
        db.add_all(
            [
                ChatMessage(
                    message_id="u1",
                    chat_id="source",
                    chat_seq=1,
                    role="user",
                    content="Remember violet",
                    extra_data={"attachments": [{"file_id": "f1"}]},
                ),
                ChatMessage(
                    message_id="a1",
                    chat_id="source",
                    chat_seq=2,
                    role="assistant",
                    content="Violet remembered.",
                    usage={"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
                    model_steps=[
                        {
                            "schema": "harness.steps.v1",
                            "kind": "assistant",
                            "blocks": [{"type": "text", "text": "Violet remembered."}],
                        }
                    ],
                    extra_data={
                        "segments": [{"type": "text", "text": "Violet remembered."}],
                        "run_id": "old",
                        "in_flight": {"run_id": "old"},
                        "plan_id": "source-plan",
                    },
                ),
                ChatMessage(
                    message_id="u2",
                    chat_id="source",
                    chat_seq=3,
                    role="user",
                    content="Future secret",
                ),
                ChatMessage(
                    message_id="a2",
                    chat_id="source",
                    chat_seq=4,
                    role="assistant",
                    content="Future answer",
                ),
            ]
        )
        db.commit()

    def database():
        with sessions() as db:
            yield db

    user = UserContext(user_id="owner", user_center_id="owner", username="Owner")
    app = FastAPI()
    app.include_router(router)
    app.include_router(chats_router)
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        yield client, sessions, user
    engine.dispose()
