"""消息落库收口的 NUL 清洗（ChatMessageRepository）。

PostgreSQL 的 text/JSONB 不接受 NUL：工具结果（PDF 提取文本等）带字面 NUL
时，整条 INSERT 会被 UntranslatableCharacter 拒绝、消息丢失（定时任务
「arXiv 雷达」2026-07-03 实际翻车过）。repository 的 create/update/
update_extra_data 是消息写库唯一收口，须在此剥离。SQLite 测试库本身不
拒绝 NUL，所以断言的是「存进去的值已被清洗」。
"""

from core.db.models import ChatSession
from core.db.repository.chat import ChatMessageRepository, _strip_nul


def _repo(db_session) -> ChatMessageRepository:
    if db_session.get(ChatSession, "chat_nul") is None:
        db_session.add(
            ChatSession(chat_id="chat_nul", user_id="user_nul", title="NUL test")
        )
        db_session.commit()
    return ChatMessageRepository(db_session)


def test_strip_nul_recurses_and_preserves_clean_values():
    assert _strip_nul("a\x00b") == "ab"
    nested = {"k\x00ey": ["x\x00", {"n": "y\x00z"}, 1, None, 3.5]}
    assert _strip_nul(nested) == {"key": ["x", {"n": "yz"}, 1, None, 3.5]}
    clean = "clean"
    assert _strip_nul(clean) is clean  # 无 NUL 时不复制


def test_create_sanitizes_content_and_json_fields(db_session):
    repo = _repo(db_session)
    msg = repo.create(
        {
            "message_id": "msg_nul_create",
            "chat_id": "chat_nul",
            "role": "assistant",
            "content": "Ψ(HA) ≡ β\x00…",
            "tool_calls": [{"tool_name": "search", "result": "pdf\x00text"}],
            "usage": {"prompt_tokens": 1},
            "extra_data": {"note": "a\x00b"},
        }
    )
    assert msg.content == "Ψ(HA) ≡ β…"
    assert msg.tool_calls == [{"tool_name": "search", "result": "pdftext"}]
    assert msg.extra_data == {"note": "ab"}


def test_update_and_extra_data_merge_sanitize(db_session):
    repo = _repo(db_session)
    repo.create(
        {
            "message_id": "msg_nul_update",
            "chat_id": "chat_nul",
            "role": "assistant",
            "content": "初始",
        }
    )
    updated = repo.update(
        "msg_nul_update",
        {"content": "新\x00正文", "tool_calls": [{"r": "v\x00"}]},
    )
    assert updated.content == "新正文"
    assert updated.tool_calls == [{"r": "v"}]

    patched = repo.update_extra_data("msg_nul_update", {"follow_up": ["q\x001"]})
    assert patched.extra_data["follow_up"] == ["q1"]
