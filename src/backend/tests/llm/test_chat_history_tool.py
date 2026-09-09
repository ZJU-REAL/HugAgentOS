"""智能体读取历史会话的两个工具（list_related_chats / read_chat）。

重点盯两件事：工具的作用域由注册时的闭包锁死（模型编一个 chat_id 也读不到别人的会话），
以及摘要生成一次之后落缓存、不重复烧模型调用。
"""

from __future__ import annotations

import json

import pytest
from core.db.engine import Base
from core.db.models import ChatCompactionState, ChatMessage, ChatSession, Project
from core.llm.tools.chat_history_tool import register_chat_history_tools
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class FakeToolkit:
    """只记下注册进来的函数——工具契约就是"注册了什么名字、调用返回什么"。"""

    def __init__(self):
        self.tools = {}

    def register_tool_function(self, fn, **_kwargs):
        self.tools[fn.__name__] = fn


def _payload(response):
    block = response.content[0]
    text = block["text"] if isinstance(block, dict) else block.text
    return json.loads(text)


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'tool.db'}", connect_args={"check_same_thread": False}
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
    Session = sessionmaker(bind=engine)
    with Session() as db:
        db.add_all(
            [
                ChatSession(
                    chat_id="past", user_id="u1", title="落户积分", project_id="p1", message_count=3
                ),
                ChatSession(
                    chat_id="now", user_id="u1", title="当前", project_id="p1", message_count=0
                ),
                ChatSession(
                    chat_id="alien", user_id="u2", title="别人的", project_id="p1", message_count=1
                ),
            ]
        )
        db.add_all(
            [
                ChatMessage(
                    message_id="a1",
                    chat_id="past",
                    role="user",
                    chat_seq=1,
                    content="杭州落户积分怎么算",
                ),
                ChatMessage(
                    message_id="a2",
                    chat_id="past",
                    role="assistant",
                    chat_seq=2,
                    content="按学历、社保、居住三项累计。",
                ),
                ChatMessage(
                    message_id="a3",
                    chat_id="past",
                    role="user",
                    chat_seq=3,
                    content="社保断缴怎么办",
                ),
                ChatMessage(
                    message_id="b1", chat_id="alien", role="user", chat_seq=1, content="机密内容"
                ),
            ]
        )
        db.commit()

    import core.db.engine as engine_module

    monkeypatch.setattr(engine_module, "SessionLocal", Session)

    toolkit = FakeToolkit()
    register_chat_history_tools(toolkit, user_id="u1", chat_id="now", project_id="p1")
    return toolkit


def test_both_tools_are_registered(wired):
    assert set(wired.tools) == {"list_related_chats", "read_chat"}


def test_registration_is_skipped_without_a_user():
    toolkit = FakeToolkit()
    register_chat_history_tools(toolkit, user_id="")
    assert toolkit.tools == {}


@pytest.mark.asyncio
async def test_listing_stays_in_project_and_excludes_the_running_chat(wired):
    data = _payload(await wired.tools["list_related_chats"]())

    assert data["ok"] is True
    assert data["scope"] == "project"
    assert [c["chat_id"] for c in data["chats"]] == ["past"]
    # 列表只给标题级信息，正文靠 read_chat 取
    assert "overview" not in data["chats"][0]


@pytest.mark.asyncio
async def test_listing_accepts_a_keyword(wired):
    hit = _payload(await wired.tools["list_related_chats"](query="社保断缴"))
    miss = _payload(await wired.tools["list_related_chats"](query="毫不相干"))

    assert [c["chat_id"] for c in hit["chats"]] == ["past"]
    assert miss["chats"] == []


@pytest.mark.asyncio
async def test_read_returns_paged_messages(wired):
    first = _payload(await wired.tools["read_chat"](chat_id="past", limit=2))

    assert first["ok"] is True
    assert first["mode"] == "full"
    assert first["title"] == "落户积分"
    assert [m["content"] for m in first["messages"]] == [
        "杭州落户积分怎么算",
        "按学历、社保、居住三项累计。",
    ]
    assert first["has_more"] is True

    rest = _payload(await wired.tools["read_chat"](chat_id="past", offset=first["next_offset"]))
    assert [m["content"] for m in rest["messages"]] == ["社保断缴怎么办"]
    assert rest["has_more"] is False


@pytest.mark.asyncio
async def test_read_card_mode_gives_the_overview(wired):
    card = _payload(await wired.tools["read_chat"](chat_id="past", mode="card"))

    assert card["mode"] == "card"
    assert "杭州落户积分怎么算" in card["overview"]


@pytest.mark.asyncio
async def test_another_users_chat_is_refused_even_with_a_valid_id(wired):
    denied = _payload(await wired.tools["read_chat"](chat_id="alien"))

    assert denied["ok"] is False
    assert "无权访问" in denied["error"]
    assert "机密内容" not in json.dumps(denied, ensure_ascii=False)


@pytest.mark.asyncio
async def test_bad_arguments_are_rejected_with_a_usable_message(wired):
    empty = _payload(await wired.tools["read_chat"](chat_id="  "))
    bad_mode = _payload(await wired.tools["read_chat"](chat_id="past", mode="whatever"))

    assert empty["ok"] is False
    assert bad_mode["ok"] is False
    assert "full / digest / card" in bad_mode["error"]


@pytest.mark.asyncio
async def test_digest_is_generated_once_then_served_from_cache(wired, monkeypatch):
    import core.services.compaction_service as compaction

    calls = {"n": 0}

    async def fake_summarize(_history, *, timeout):  # noqa: ARG001
        calls["n"] += 1
        return "这段会话讨论了杭州落户积分的构成与社保断缴的处理。"

    monkeypatch.setattr(compaction, "_summarize", fake_summarize)

    first = _payload(await wired.tools["read_chat"](chat_id="past", mode="digest"))
    second = _payload(await wired.tools["read_chat"](chat_id="past", mode="digest"))

    assert first["digest"].startswith("这段会话讨论了")
    assert first["cached"] is False
    assert second["cached"] is True
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_digest_failure_points_at_the_full_text_path(wired, monkeypatch):
    import core.services.compaction_service as compaction

    async def failing_summarize(_history, *, timeout):  # noqa: ARG001
        return None

    monkeypatch.setattr(compaction, "_summarize", failing_summarize)

    result = _payload(await wired.tools["read_chat"](chat_id="past", mode="digest"))

    assert result["digest"] == ""
    assert "mode='full'" in result["note"]
