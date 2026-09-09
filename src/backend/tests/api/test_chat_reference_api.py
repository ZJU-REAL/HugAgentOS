"""会话引用在接口层的两条契约。

一是挑选接口按当前用户和项目范围收口；二是**发送时渲染的名片和历史重放时渲染的名片
必须一字不差**——两边分别拼一次，刷新一下模型看到的引用就变了，同一轮对话前后不一致。
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from core.auth.backend import UserContext
from core.db.engine import Base
from core.db.models import ChatCompactionState, ChatMessage, ChatSession, Project
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture()
def db_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'reference.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        engine,
        tables=[
            ChatSession.__table__,
            ChatMessage.__table__,
            ChatCompactionState.__table__,
            Project.__table__,
        ],
    )
    return sessionmaker(bind=engine)


def _seed(Session):
    with Session() as db:
        db.add_all(
            [
                ChatSession(
                    chat_id="p-old",
                    user_id="u1",
                    title="项目内旧会话",
                    project_id="p1",
                    message_count=2,
                ),
                ChatSession(
                    chat_id="p-now",
                    user_id="u1",
                    title="当前会话",
                    project_id="p1",
                    message_count=1,
                ),
                ChatSession(chat_id="loose", user_id="u1", title="项目外会话", message_count=1),
                ChatSession(
                    chat_id="alien",
                    user_id="u2",
                    title="别人的会话",
                    project_id="p1",
                    message_count=1,
                ),
            ]
        )
        db.add_all(
            [
                ChatMessage(
                    message_id="m1",
                    chat_id="p-old",
                    role="user",
                    chat_seq=1,
                    content="杭州落户积分怎么算",
                ),
                ChatMessage(
                    message_id="m2",
                    chat_id="p-old",
                    role="assistant",
                    chat_seq=2,
                    content="按学历、社保、居住三项累计。",
                ),
                ChatMessage(
                    message_id="m3", chat_id="alien", role="user", chat_seq=1, content="机密内容"
                ),
            ]
        )
        db.commit()


def _app(Session, *, user_id="u1"):
    import api.routes.v1.chats as chats

    app = FastAPI()
    app.include_router(chats.router)
    app.dependency_overrides[chats.get_current_user] = lambda: UserContext(
        user_id=user_id, user_center_id=user_id, username=user_id
    )

    def _db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[chats.get_db] = _db
    return app


@pytest.mark.asyncio
async def test_referencable_endpoint_scopes_to_project_and_owner(db_factory, monkeypatch):
    import api.routes.v1.chats as chats

    monkeypatch.setattr(chats, "resolve_db_user_id", lambda _db, user_id: user_id)
    _seed(db_factory)

    transport = httpx.ASGITransport(app=_app(db_factory))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/chats/referencable?project_id=p1&exclude_chat_id=p-now")

    assert resp.status_code == 200
    items = resp.json()["data"]["items"]
    assert [i["chat_id"] for i in items] == ["p-old"]
    assert items[0]["title"] == "项目内旧会话"
    # 挑选阶段不下发正文，只有标题级信息
    assert "overview" not in items[0]


@pytest.mark.asyncio
async def test_referencable_endpoint_search_matches_message_text(db_factory, monkeypatch):
    import api.routes.v1.chats as chats

    monkeypatch.setattr(chats, "resolve_db_user_id", lambda _db, user_id: user_id)
    _seed(db_factory)

    transport = httpx.ASGITransport(app=_app(db_factory))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        hit = await client.get("/v1/chats/referencable?q=落户积分")
        miss = await client.get("/v1/chats/referencable?q=不存在的词")

    assert [i["chat_id"] for i in hit.json()["data"]["items"]] == ["p-old"]
    assert miss.json()["data"]["items"] == []


@pytest.mark.asyncio
async def test_another_user_sees_none_of_it(db_factory, monkeypatch):
    import api.routes.v1.chats as chats

    monkeypatch.setattr(chats, "resolve_db_user_id", lambda _db, user_id: user_id)
    _seed(db_factory)

    transport = httpx.ASGITransport(app=_app(db_factory, user_id="u3"))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/chats/referencable")

    assert resp.json()["data"]["items"] == []


def test_send_time_and_replay_render_the_same_reference_block(db_factory):
    """引用名片在发送时落库，重放时按落库的快照渲染——两边必须完全一致。"""
    import api.routes.v1.chats as chats
    from api.schemas import ChatRequest
    from core.chat.context import build_effective_user_message
    from core.services.compaction_service import _normalize_rows

    _seed(db_factory)

    request = ChatRequest(
        chat_id="p-now",
        message="接着上次那段继续说",
        referenced_chats=[{"chat_id": "p-old"}],
    )

    with db_factory() as db:
        block = chats._resolve_reference_block(db, request, "u1")
        extra = chats._build_user_extra_data(request)

    assert "项目内旧会话" in block
    assert "chat_id=p-old" in block
    assert [c["chat_id"] for c in extra["referenced_chats"]] == ["p-old"]

    sent = build_effective_user_message(request.message, None, block)

    replayed = _normalize_rows(
        [
            SimpleNamespace(
                role="user",
                extra_data=extra,
                content=request.message,
                tool_calls=None,
            )
        ]
    )

    assert replayed[0]["content"] == sent


def test_unreadable_reference_is_dropped_instead_of_leaking(db_factory):
    import api.routes.v1.chats as chats
    from api.schemas import ChatRequest

    _seed(db_factory)

    request = ChatRequest(
        chat_id="p-now",
        message="看看这个",
        referenced_chats=[{"chat_id": "alien"}],
    )

    with db_factory() as db:
        block = chats._resolve_reference_block(db, request, "u1")
        extra = chats._build_user_extra_data(request)

    assert block == ""
    assert "referenced_chats" not in extra
