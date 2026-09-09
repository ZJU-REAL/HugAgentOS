"""跨会话引用的契约测试。

这几条盯的是三件必须成立的事：**别人的会话读不到**、引用注入的是名片而不是全文、
以及名片文本在发送和历史重放两条路上完全一致（不一致的话，同一轮对话刷新前后模型
看到的东西会变）。
"""

from __future__ import annotations

import pytest
from core.chat.context import build_effective_user_message
from core.db.models import ChatMessage, ChatSession
from core.services.chat_reference_service import (
    OVERVIEW_MAX_CHARS,
    READ_TOTAL_MAX_CHARS,
    list_referencable_sessions,
    read_session_messages,
    render_reference_block,
    resolve_reference_cards,
    session_brief,
    session_card,
)


def _chat(db, chat_id: str, *, user_id: str = "u1", title: str = "会话", project_id=None):
    session = ChatSession(
        chat_id=chat_id,
        user_id=user_id,
        title=title,
        project_id=project_id,
        message_count=0,
    )
    db.add(session)
    db.commit()
    return session


def _say(db, chat_id: str, role: str, content: str, seq: int):
    db.add(
        ChatMessage(
            message_id=f"{chat_id}-{seq}",
            chat_id=chat_id,
            role=role,
            chat_seq=seq,
            content=content,
        )
    )
    session = db.get(ChatSession, chat_id)
    session.message_count = (session.message_count or 0) + 1
    db.commit()


def test_listing_stays_inside_the_project_and_skips_the_current_chat(db_session):
    _chat(db_session, "c-in", project_id="p1", title="项目内")
    _chat(db_session, "c-out", project_id=None, title="项目外")
    _chat(db_session, "c-self", project_id="p1", title="当前会话")

    listed = list_referencable_sessions(db_session, "u1", project_id="p1", exclude_chat_id="c-self")

    assert [s.chat_id for s in listed] == ["c-in"]


def test_listing_never_crosses_users(db_session):
    _chat(db_session, "mine", user_id="u1", title="我的")
    _chat(db_session, "theirs", user_id="u2", title="别人的")

    listed = list_referencable_sessions(db_session, "u1")

    assert [s.chat_id for s in listed] == ["mine"]


def test_listing_matches_message_content_not_just_titles(db_session):
    _chat(db_session, "c1", title="无关标题")
    _say(db_session, "c1", "user", "帮我算一下杭州的落户积分", 1)
    _chat(db_session, "c2", title="另一段")
    _say(db_session, "c2", "user", "写一封邮件", 1)

    listed = list_referencable_sessions(db_session, "u1", query="落户积分")

    assert [s.chat_id for s in listed] == ["c1"]


def test_card_overview_falls_back_to_first_question_and_last_answer(db_session):
    _chat(db_session, "c1", title="积分咨询")
    _say(db_session, "c1", "user", "杭州落户积分怎么算", 1)
    _say(db_session, "c1", "assistant", "按学历、社保、居住三项累计。", 2)

    card = session_card(db_session, "c1", "u1")

    assert card["title"] == "积分咨询"
    assert card["message_count"] == 2
    assert card["overview_kind"] == "excerpt"
    assert "杭州落户积分怎么算" in card["overview"]
    assert "按学历、社保、居住三项累计。" in card["overview"]


def test_card_overview_is_bounded(db_session):
    _chat(db_session, "c1")
    _say(db_session, "c1", "user", "问" * 5000, 1)
    _say(db_session, "c1", "assistant", "答" * 5000, 2)

    card = session_card(db_session, "c1", "u1")

    assert len(card["overview"]) <= OVERVIEW_MAX_CHARS + 1  # 末尾的省略号


def test_card_and_read_refuse_another_users_chat(db_session):
    _chat(db_session, "theirs", user_id="u2")
    _say(db_session, "theirs", "user", "机密", 1)

    assert session_card(db_session, "theirs", "u1") is None
    assert read_session_messages(db_session, "theirs", "u1") is None


def test_read_pages_through_messages(db_session):
    _chat(db_session, "c1")
    for i in range(1, 6):
        _say(db_session, "c1", "user" if i % 2 else "assistant", f"第{i}条", i)

    first = read_session_messages(db_session, "c1", "u1", offset=0, limit=2)

    assert first["total_messages"] == 5
    assert [m["content"] for m in first["messages"]] == ["第1条", "第2条"]
    assert first["has_more"] is True
    assert first["next_offset"] == 2

    last = read_session_messages(db_session, "c1", "u1", offset=4, limit=2)

    assert [m["content"] for m in last["messages"]] == ["第5条"]
    assert last["has_more"] is False
    assert last["next_offset"] is None


def test_read_respects_the_total_character_budget(db_session):
    _chat(db_session, "c1")
    for i in range(1, 11):
        _say(db_session, "c1", "user", "字" * 2000, i)

    page = read_session_messages(db_session, "c1", "u1", offset=0, limit=10)

    used = sum(len(m["content"]) for m in page["messages"])
    assert used <= READ_TOTAL_MAX_CHARS
    assert page["truncated_by_budget"] is True


def test_read_hides_internal_compaction_rows(db_session):
    _chat(db_session, "c1")
    _say(db_session, "c1", "user", "正常消息", 1)
    _say(db_session, "c1", "system", "压缩检查点摘要", 2)

    page = read_session_messages(db_session, "c1", "u1")

    assert [m["content"] for m in page["messages"]] == ["正常消息"]
    assert page["total_messages"] == 1


def test_resolve_drops_references_the_user_cannot_read(db_session):
    _chat(db_session, "mine", title="我的")
    _say(db_session, "mine", "user", "内容", 1)
    _chat(db_session, "theirs", user_id="u2", title="别人的")

    cards = resolve_reference_cards(
        db_session, "u1", [{"chat_id": "mine"}, {"chat_id": "theirs"}, {"chat_id": "mine"}]
    )

    assert [c["chat_id"] for c in cards] == ["mine"]


def test_reference_block_carries_the_card_and_points_at_the_tool(db_session):
    _chat(db_session, "mine", title="落户积分")
    _say(db_session, "mine", "user", "杭州落户积分怎么算", 1)

    cards = resolve_reference_cards(db_session, "u1", [{"chat_id": "mine"}])
    block = render_reference_block(cards)

    assert "落户积分" in block
    assert "chat_id=mine" in block
    assert "read_chat" in block
    # 名片给的是概览，不是把整段会话原样贴进来
    assert "【引用会话】" in block


def test_reference_block_is_empty_without_references():
    assert render_reference_block(None) == ""
    assert render_reference_block([]) == ""
    assert render_reference_block([{"title": "缺 id"}]) == ""


def test_effective_message_puts_references_before_quote_and_question():
    block = render_reference_block(
        [
            {
                "chat_id": "c1",
                "title": "旧会话",
                "last_active_display": "2026-01-01 10:00",
                "message_count": 3,
                "overview": "聊过落户积分",
            }
        ]
    )

    merged = build_effective_user_message("接着上次说", {"text": "原文"}, block)

    assert merged.index("【引用会话】") < merged.index("【引用原文】")
    assert merged.index("【引用原文】") < merged.index("【用户追问】")
    # 没有引用时行为完全不变
    assert build_effective_user_message("你好", None, "") == "你好"


def test_brief_is_a_pure_mapping(db_session):
    session = _chat(db_session, "c1", title="标题")
    session.message_count = 7
    db_session.commit()

    brief = session_brief(session)

    assert brief["chat_id"] == "c1"
    assert brief["title"] == "标题"
    assert brief["message_count"] == 7
    assert "overview" not in brief


@pytest.mark.parametrize("mode", ["excerpt"])
def test_card_of_an_empty_chat_does_not_explode(db_session, mode):
    _chat(db_session, "empty", title="空会话")

    card = session_card(db_session, "empty", "u1")

    assert card["message_count"] == 0
    assert card["overview"] == ""
    assert card["overview_kind"] == mode
