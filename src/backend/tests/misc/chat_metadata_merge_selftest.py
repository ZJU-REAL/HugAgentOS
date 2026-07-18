#!/usr/bin/env python3
"""Regression tests for chat session metadata merge semantics."""

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from core.services import ChatService


def test_update_session_merges_mode_flags_into_extra_data(db_session):
    """Updating business metadata must preserve mode-identifying flags."""
    chat_service = ChatService(db_session)

    session = chat_service.create_session(
        "user_code_exec",
        "代码执行测试",
        extra_data={
            "businessTopic": "综合咨询",
            "code_exec_chat": True,
            "plan_chat": True,
            "agent_id": "agent_demo",
            "agent_name": "示例子智能体",
        },
    )

    updated = chat_service.update_session(
        session.chat_id,
        "user_code_exec",
        {"extra_data": {"businessTopic": "产业分析"}},
    )

    assert updated is not None
    assert updated.extra_data["businessTopic"] == "产业分析"
    assert updated.extra_data["code_exec_chat"] is True
    assert updated.extra_data["plan_chat"] is True
    assert updated.extra_data["agent_id"] == "agent_demo"
    assert updated.extra_data["agent_name"] == "示例子智能体"


def test_update_session_still_updates_scalar_fields(db_session):
    """Metadata merge should not break regular scalar session updates."""
    chat_service = ChatService(db_session)

    session = chat_service.create_session(
        "user_merge_scalar",
        "旧标题",
        extra_data={"code_exec_chat": True},
    )

    updated = chat_service.update_session(
        session.chat_id,
        "user_merge_scalar",
        {
            "title": "新标题",
            "favorite": True,
            "extra_data": {"businessTopic": "运行调试"},
        },
    )

    assert updated is not None
    assert updated.title == "新标题"
    assert updated.favorite is True
    assert updated.extra_data["businessTopic"] == "运行调试"
    assert updated.extra_data["code_exec_chat"] is True
