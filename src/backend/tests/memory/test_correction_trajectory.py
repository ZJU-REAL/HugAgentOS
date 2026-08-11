"""Multi-turn correction evidence for procedural memory."""

from datetime import datetime, timedelta

from core.db.models import ChatMessage, ChatSession, UserShadow
from core.db.repository import ChatMessageRepository
from core.memory.trajectory import is_verified_correction


def test_failure_user_method_change_and_success_is_verified_correction():
    messages = [
        {
            "role": "assistant",
            "content": "沙盒环境无网络，无法用 curl 下载这个 PDF。",
        },
        {
            "role": "user",
            "content": "你把 OSS 链接下载下来，放到沙盒里面然后交付给我。",
        },
        {
            "role": "assistant",
            "content": "下载成功了，已验证 PDF 文件完整并完成交付。",
        },
        {
            "role": "user",
            "content": "那为什么之前一直失败？",
        },
        {
            "role": "assistant",
            "content": "之前工具选型错误；正确做法是用 curl 下载后再校验。",
        },
    ]

    assert is_verified_correction(messages) is True


def test_confident_wrong_result_user_correction_and_success_is_verified():
    messages = [
        {
            "role": "assistant",
            "content": "已按订单总额直接汇总，本月收入为 120 万元。",
        },
        {
            "role": "user",
            "content": "不对，应该先扣除退款订单再汇总，否则和财务口径对不上。",
        },
        {
            "role": "assistant",
            "content": "已先扣除退款订单并重新汇总，结果验证成功，与财务数据一致。",
        },
    ]

    assert is_verified_correction(messages) is True


def test_bare_retry_after_transient_failure_is_not_a_reusable_correction():
    messages = [
        {"role": "assistant", "content": "服务暂时不可用，本次请求失败。"},
        {"role": "user", "content": "再试一次"},
        {"role": "assistant", "content": "重试成功。"},
    ]

    assert is_verified_correction(messages) is False


def test_assistant_self_declared_lesson_without_user_method_change_is_not_verified():
    messages = [
        {"role": "user", "content": "帮我下载文件"},
        {"role": "assistant", "content": "下载成功。以后我认为都应该用 curl。"},
    ]

    assert is_verified_correction(messages) is False


def test_new_method_request_without_failure_or_explicit_correction_is_not_verified():
    messages = [
        {"role": "assistant", "content": "这里是第一版分析结果。"},
        {"role": "user", "content": "你可以再用另一种方法分析一次。"},
        {"role": "assistant", "content": "第二种分析也已完成。"},
    ]

    assert is_verified_correction(messages) is False


def test_recent_message_repository_honors_boundary_limit_and_visible_roles(db_session):
    base = datetime(2026, 8, 10, 12, 0, 0)
    db_session.add(UserShadow(user_id="trajectory-user", username="Trajectory User"))
    db_session.add(
        ChatSession(chat_id="trajectory-chat", user_id="trajectory-user", title="trajectory")
    )
    rows = [
        ChatMessage(
            message_id="m1",
            chat_id="trajectory-chat",
            role="user",
            content="first",
            created_at=base,
        ),
        ChatMessage(
            message_id="m2",
            chat_id="trajectory-chat",
            role="assistant",
            content="second",
            created_at=base + timedelta(seconds=1),
        ),
        ChatMessage(
            message_id="checkpoint",
            chat_id="trajectory-chat",
            role="system",
            content="internal",
            created_at=base + timedelta(seconds=2),
        ),
        ChatMessage(
            message_id="m3",
            chat_id="trajectory-chat",
            role="user",
            content="third",
            created_at=base + timedelta(seconds=3),
        ),
        ChatMessage(
            message_id="m4",
            chat_id="trajectory-chat",
            role="assistant",
            content="later overlapping turn",
            created_at=base + timedelta(seconds=4),
        ),
    ]
    db_session.add_all(rows)
    db_session.commit()

    recent = ChatMessageRepository(db_session).list_recent_by_chat(
        "trajectory-chat",
        limit=2,
        before=base + timedelta(seconds=3),
    )

    assert [row.message_id for row in recent] == ["m2", "m3"]
