"""Local-only HTTP fixture: real chat routes/SQL, synthetic identities and data.

Run from the project root with PYTHONPATH selecting before/after backend source.
No model provider, Redis, production database or production credentials required.
"""

import argparse
import asyncio
import json
import os
import time
from types import SimpleNamespace

import core.services as chat_services
import uvicorn
from api.routes.v1 import chats
from core.auth.backend import get_current_user
from core.db.engine import Base, get_db
from core.db.models import ChatMessage, ChatSession, UserShadow
from core.llm.tools import user_questions
from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--seed", action="store_true")
args = parser.parse_args()
url = os.environ["DATABASE_URL"]
assert "127.0.0.1:55472/perf" in url or "/performance-172/" in url
engine = create_engine(url, pool_size=8, max_overflow=8)
Session = sessionmaker(bind=engine)
if args.seed:
    Base.metadata.create_all(engine)
    with Session() as db:
        if db.get(ChatSession, "perf-history") is None:
            db.merge(UserShadow(user_id="perf-user", username="perf"))
            db.merge(UserShadow(user_id="other-user", username="other"))
            db.flush()
            db.add(
                ChatSession(
                    chat_id="perf-history",
                    user_id="perf-user",
                    title="Performance history",
                    message_count=400,
                    pinned=True,
                )
            )
            for i in range(400):
                tools = (
                    [
                        {
                            "id": f"tool-{i}",
                            "name": "read_file",
                            "result": {"text": "数据" * 500_000, "items": [1, 2]},
                        }
                    ]
                    if i % 5 == 0
                    else None
                )
                db.add(
                    ChatMessage(
                        message_id=f"m{i+1:04}",
                        chat_id="perf-history",
                        chat_seq=i + 1,
                        role="assistant" if i % 2 else "user",
                        content=f"Performance message {i+1:04}\n\n" + "A useful paragraph. " * 20,
                        tool_calls=tools,
                        model_steps=tools,
                        extra_data={},
                    )
                )
            for i in range(200):
                db.add(
                    ChatSession(
                        chat_id=f"search-{i:03}", user_id="perf-user", title=f"Conversation {i:03}"
                    )
                )
                db.add(
                    ChatMessage(
                        message_id=f"search-m{i}",
                        chat_id=f"search-{i:03}",
                        chat_seq=1,
                        role="assistant",
                        content="visible performance needle",
                    )
                )
            db.add(
                ChatSession(chat_id="foreign-chat", user_id="other-user", title="private needle")
            )
            db.commit()
            from core.infra.time import utc_now

            db.get(ChatSession, "perf-history").updated_at = utc_now()
            db.commit()

sql = []


@event.listens_for(engine, "before_cursor_execute")
def record_sql(conn, cur, statement, parameters, ctx, many):
    if len(sql) < 10000:
        sql.append(statement)


app = FastAPI()
from api.middleware.error_handler import setup_error_handlers

setup_error_handlers(app)
app.include_router(chats.router)


def db_dependency():
    with Session() as db:
        yield db


app.dependency_overrides[get_db] = db_dependency
app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
    user_id="perf-user", username="perf"
)


@app.get("/__perf/health")
async def health():
    return {"ok": True}


@app.get("/__perf/sql")
def statements():
    result = list(sql)
    sql.clear()
    return result


@app.post("/__perf/append")
def append():
    with Session() as db:
        from sqlalchemy import func

        last = (
            db.query(func.max(ChatMessage.chat_seq))
            .filter(ChatMessage.chat_id == "perf-history")
            .scalar()
        )
        for i in range(last + 1, last + 41):
            db.add(
                ChatMessage(
                    message_id=f"m{i:04}",
                    chat_id="perf-history",
                    chat_seq=i,
                    role="assistant",
                    content=f"Performance message {i:04}",
                )
            )
        db.commit()
        return {"last": last + 40}


@app.get("/__perf/stream")
async def stream():
    from starlette.responses import StreamingResponse

    async def chunks():
        for i in range(40):
            yield "data: " + json.dumps({"text": f" token{i}"}) + "\n\n"
            await asyncio.sleep(0.02)

    return StreamingResponse(chunks(), media_type="text/event-stream")


# Isolate the slow database boundary for an HTTP concurrency experiment. The
# real get_pending_user_questions route/worker scheduling remains unchanged.
OriginalService = chat_services.ChatService


class MeasuredService(OriginalService):
    def get_session(self, chat_id, user_id):
        if chat_id == "slow-perf":
            time.sleep(0.2)
            return SimpleNamespace(chat_id=chat_id)
        return super().get_session(chat_id, user_id)


chat_services.ChatService = MeasuredService


async def no_questions(chat_id):
    return []


user_questions.get_all_pending_shared = no_questions
uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
