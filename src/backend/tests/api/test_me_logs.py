"""Unit tests for personal call logs (/v1/me/logs).

Covers: self-only filtering (list shows only your own rows), cross-user detail access 404,
subagent detail subtree aggregation, and personal usage detail & summary (accumulated on
the Python side, independent of SQLite/PG dialect).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from core.auth.backend import UserContext
from core.db.models import ChatMessage, ChatSession, SubAgentCallLog, ToolCallLog


def _ctx(user_id: str) -> UserContext:
    return UserContext(user_id=user_id, user_center_id=user_id, username=user_id)


@pytest.fixture
def seeded(db_session):
    """One tool log for each of two users + one subagent tree for alice + two chat messages for alice."""
    now = datetime.utcnow()
    db_session.add_all(
        [
            ToolCallLog(id="t_alice", user_id="alice", tool_name="internet_search",
                        status="success", source="main_agent", chat_id="c1",
                        created_at=now),
            ToolCallLog(id="t_bob", user_id="bob", tool_name="bash",
                        status="failed", source="main_agent", created_at=now),
            SubAgentCallLog(id="sa_root", user_id="alice", subagent_name="planner",
                            status="success", created_at=now),
            SubAgentCallLog(id="sa_child", user_id="alice", subagent_name="planner",
                            status="success", parent_subagent_log_id="sa_root",
                            step_index=1, created_at=now),
            ToolCallLog(id="t_sub", user_id="alice", tool_name="read_file",
                        status="success", source="subagent",
                        subagent_log_id="sa_child", created_at=now),
            ChatSession(chat_id="c1", user_id="alice", title="会话一"),
            ChatSession(chat_id="c2", user_id="bob", title="别人的"),
            ChatMessage(message_id="m1", chat_id="c1", role="assistant", content="", model="deepseek",
                        usage={"prompt_tokens": 10, "completion_tokens": 5},
                        created_at=now - timedelta(days=1)),
            ChatMessage(message_id="m2", chat_id="c1", role="assistant", content="", model="deepseek",
                        usage={"prompt_tokens": 7, "completion_tokens": 3}, created_at=now),
            ChatMessage(message_id="m3", chat_id="c2", role="assistant", content="", model="qwen",
                        usage={"prompt_tokens": 999, "completion_tokens": 999},
                        created_at=now),
        ]
    )
    db_session.commit()


# ── Tool logs: self-only filtering ─────────────────────────────────────────────────────


def test_tool_logs_only_own_rows(db_session, seeded):
    from api.routes.v1.me_logs import list_my_tool_logs

    resp = list_my_tool_logs(
        chat_id=None, tool_name=None, status=None, source=None,
        date_from=None, date_to=None, page=1, page_size=50,
        user=_ctx("alice"), db=db_session,
    )
    ids = {i["id"] for i in resp["data"]["items"]}
    assert "t_alice" in ids and "t_sub" in ids
    assert "t_bob" not in ids
    # session title is returned alongside each row
    by_id = {i["id"]: i for i in resp["data"]["items"]}
    assert by_id["t_alice"]["session_title"] == "会话一"


def test_tool_log_detail_owner_ok_foreign_404(db_session, seeded):
    from api.routes.v1.me_logs import get_my_tool_log

    resp = get_my_tool_log("t_alice", user=_ctx("alice"), db=db_session)
    assert resp["data"]["id"] == "t_alice"

    with pytest.raises(HTTPException) as exc:
        get_my_tool_log("t_bob", user=_ctx("alice"), db=db_session)
    assert exc.value.status_code == 404


# ── Subagent detail: subtree aggregation ────────────────────────────────────────────────


def test_subagent_detail_includes_subtree(db_session, seeded):
    from api.routes.v1.me_logs import get_my_subagent_log

    resp = get_my_subagent_log("sa_root", user=_ctx("alice"), db=db_session)
    data = resp["data"]
    assert [s["id"] for s in data["child_steps"]] == ["sa_child"]
    assert [t["id"] for t in data["tool_calls"]] == ["t_sub"]

    with pytest.raises(HTTPException) as exc:
        get_my_subagent_log("sa_root", user=_ctx("bob"), db=db_session)
    assert exc.value.status_code == 404


def test_subagent_list_only_parents_by_default(db_session, seeded):
    from api.routes.v1.me_logs import list_my_subagent_logs

    resp = list_my_subagent_logs(
        chat_id=None, subagent_name=None, status=None, only_parents=True,
        date_from=None, date_to=None, page=1, page_size=50,
        user=_ctx("alice"), db=db_session,
    )
    ids = {i["id"] for i in resp["data"]["items"]}
    assert ids == {"sa_root"}


# ── Personal usage ──────────────────────────────────────────────────────────────


def test_usage_list_only_own_sessions(db_session, seeded):
    from api.routes.v1.me_logs import list_my_usage

    resp = list_my_usage(
        date_from=None, date_to=None, page=1, page_size=50,
        user=_ctx("alice"), db=db_session,
    )
    items = resp["data"]["items"]
    assert {i["message_id"] for i in items} == {"m1", "m2"}
    assert all(i["model"] == "deepseek" for i in items)


def test_usage_summary_by_model(db_session, seeded):
    from api.routes.v1.me_logs import my_usage_summary

    resp = my_usage_summary(
        date_from=None, date_to=None, group_by="model",
        user=_ctx("alice"), db=db_session,
    )
    assert resp["data"] == [
        {
            "group_key": "deepseek",
            "total_requests": 2,
            "prompt_tokens": 17,
            "completion_tokens": 8,
            "total_tokens": 25,
        }
    ]
