"""Fork contract through HTTP and the normal history replay boundary."""

from uuid import uuid4

import pytest
from core.db.models import ChatCompactionState, ChatMessage, ChatRun, ChatSession
from core.services.chat_service import ChatService
from core.services.compaction_service import load_session_history
from tests.api.chat_fork_fixtures import fork_env  # noqa: F401


def test_fork_copies_prefix_as_independent_replayable_history(fork_env):
    client, sessions, _ = fork_env
    response = client.post(
        "/v1/chats/source/fork",
        json={
            "request_id": str(uuid4()),
            "through_message_id": "a1",
        },
    )
    assert response.status_code == 201, response.text
    chat = response.json()["data"]
    cid = chat["chat_id"]
    assert cid != "source"
    assert chat["message_count"] == 2
    assert chat["metadata"]["fork"]["source_message_id"] == "a1"
    messages = client.get(f"/v1/chats/{cid}/messages").json()["data"]["items"]
    assert [m["content"] for m in messages] == ["Remember violet", "Violet remembered."]
    assert {m["message_id"] for m in messages}.isdisjoint({"u1", "a1"})
    assert messages[0]["metadata"]["attachments"] == [{"file_id": "f1"}]
    assert "run_id" not in messages[1]["metadata"]
    assert "plan_id" not in messages[1]["metadata"]
    assert "plan_progress" not in chat["metadata"]
    with sessions() as db:
        copied = db.query(ChatMessage).filter_by(chat_id=cid, role="assistant").one()
        assert copied.usage is None
        assert copied.extra_data["forked_usage"]["total_tokens"] == 15
        assert db.get(ChatMessage, "a1").usage["total_tokens"] == 15
        history = load_session_history(ChatService(db), cid, "owner")
        assert "Violet remembered." in str(history)
        assert "Future secret" not in str(history)
        ChatService(db).delete_messages_from("source", "u1")
    assert client.get(f"/v1/chats/{cid}/messages").json()["data"]["items"] == messages


def fork(client, **params):
    return client.post("/v1/chats/source/fork", json={"request_id": str(uuid4()), **params})


def test_default_boundary_and_retry_are_stable(fork_env):
    client, sessions, _ = fork_env
    request_id = str(uuid4())
    first = fork(client, request_id=request_id)
    assert first.status_code == 201
    cid = first.json()["data"]["chat_id"]
    assert first.json()["data"]["message_count"] == 4
    with sessions() as db:
        db.add(
            ChatMessage(
                chat_id="source",
                message_id="a3",
                chat_seq=5,
                role="assistant",
                content="New later content",
            )
        )
        db.commit()
    retry = fork(client, request_id=request_id)
    assert retry.json()["data"]["chat_id"] == cid
    assert retry.json()["data"]["message_count"] == 4
    assert fork(client, request_id=request_id, through_message_id="a1").status_code == 409
    with sessions() as db:
        assert db.query(ChatSession).count() == 2
        assert db.get(ChatSession, "source").extra_data["plan_progress"] == {"id": "plan"}


@pytest.mark.parametrize("status", ["running", "cancelled", "needs_attention"])
def test_writer_slot_blocks_current_reply_but_allows_previous(fork_env, status):
    client, sessions, _ = fork_env
    with sessions() as db:
        db.add(
            ChatRun(
                run_id="active",
                chat_id="source",
                user_id="owner",
                message_id="a2",
                user_message_id="u2",
                status=status,
                user_chat_seq=3,
                assistant_chat_seq=4,
                writer_slot="main",
            )
        )
        db.commit()
    assert fork(client, through_message_id="a2").status_code == 409
    response = fork(client)
    assert response.status_code == 201
    assert response.json()["data"]["metadata"]["fork"]["source_message_id"] == "a1"
    cid = response.json()["data"]["chat_id"]
    with sessions() as db:
        assert db.query(ChatRun).filter_by(chat_id=cid).count() == 0


@pytest.mark.parametrize("boundary,code", [("u1", 409), ("missing", 404)])
def test_rejects_invalid_boundary_without_creating_session(fork_env, boundary, code):
    client, sessions, _ = fork_env
    assert fork(client, through_message_id=boundary).status_code == code
    with sessions() as db:
        assert db.query(ChatSession).count() == 1


def test_only_owner_can_fork_and_deleted_sources_are_rejected(fork_env):
    from datetime import datetime, timezone

    client, sessions, user = fork_env
    user.user_id = "someone-else"
    assert fork(client).status_code == 404
    user.user_id = "owner"
    with sessions() as db:
        db.get(ChatSession, "source").deleted_at = datetime.now(timezone.utc)
        db.commit()
    assert fork(client).status_code == 404


def test_empty_chat_and_all_running_history_do_not_create_branch(fork_env):
    client, sessions, _ = fork_env
    with sessions() as db:
        db.query(ChatMessage).delete()
        db.commit()
    assert fork(client).status_code == 409
    with sessions() as db:
        assert db.query(ChatSession).count() == 1


def test_future_checkpoint_is_not_inherited_and_tool_context_is_replayed(fork_env):
    client, sessions, _ = fork_env
    from core.llm.model_steps import record_assistant_step, record_tool_result_step

    steps = [
        record_assistant_step(
            [
                {"type": "thinking", "thinking": "Check the document"},
                {
                    "type": "tool_call",
                    "id": "call_original",
                    "name": "read_file",
                    "input": {"file_id": "f1"},
                },
            ],
            provider="test",
            model="test",
            protocol="openai",
        ),
        record_tool_result_step(
            {
                "type": "tool_result",
                "id": "call_original",
                "name": "read_file",
                "output": "violet from file",
            }
        ),
        record_assistant_step(
            [{"type": "text", "text": "Violet remembered."}],
            provider="test",
            model="test",
            protocol="openai",
        ),
    ]
    with sessions() as db:
        row = db.get(ChatMessage, "a1")
        row.model_steps = steps
        row.extra_data = {
            **row.extra_data,
            "plan_snapshot": {"title": "Old plan"},
            "quoted_follow_up": {"message_id": "u1"},
            "agent_id": "agent_1",
            "agent_profile": {"name": "Reader"},
        }
        db.add(
            ChatMessage(
                chat_id="source",
                message_id="checkpoint",
                chat_seq=5,
                role="system",
                content="Future secret summary",
            )
        )
        db.flush()
        db.add(
            ChatCompactionState(
                chat_id="source",
                active_checkpoint_id="checkpoint",
                covered_seq=4,
                checkpoint_version=1,
            )
        )
        db.commit()
    response = fork(client, through_message_id="a1")
    assert response.status_code == 201
    cid = response.json()["data"]["chat_id"]
    with sessions() as db:
        rows = db.query(ChatMessage).filter_by(chat_id=cid).order_by(ChatMessage.chat_seq).all()
        assert rows[1].model_steps == steps
        assert rows[1].extra_data["quoted_follow_up"]["message_id"] == rows[0].message_id
        assert rows[1].extra_data["plan_snapshot"] == {"title": "Old plan"}
        assert rows[1].extra_data["forked_history"] is True
        assert rows[1].extra_data["agent_profile"] == {"name": "Reader"}
        assert db.get(ChatCompactionState, cid) is None
        history = load_session_history(ChatService(db), cid, "owner")
        rendered = str(history)
        assert "violet from file" in rendered and "call_original" in rendered
        assert "Future secret" not in rendered


def test_project_access_is_checked_on_creation_and_retry(fork_env):
    from core.db.models import Project

    client, sessions, _ = fork_env
    with sessions() as db:
        db.add(Project(project_id="p1", name="Personal", kind="personal", owner_user_id="owner"))
        db.get(ChatSession, "source").project_id = "p1"
        db.commit()
    request_id = str(uuid4())
    first = fork(client, request_id=request_id)
    assert first.status_code == 201
    assert first.json()["data"]["project_id"] == "p1"
    with sessions() as db:
        db.get(Project, "p1").owner_user_id = "someone-else"
        db.commit()
    assert fork(client, request_id=request_id).status_code == 404
    assert fork(client).status_code == 404


def test_concurrent_retries_create_one_complete_branch(fork_env):
    from concurrent.futures import ThreadPoolExecutor

    client, sessions, _ = fork_env
    request_id = str(uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: fork(client, request_id=request_id), range(4)))
    assert [r.status_code for r in responses] == [201] * 4
    assert len({r.json()["data"]["chat_id"] for r in responses}) == 1
    with sessions() as db:
        assert db.query(ChatSession).count() == 2
        assert db.query(ChatMessage).count() == 8


def test_failure_rolls_back_session_and_all_copied_rows(fork_env):
    from sqlalchemy import event

    client, sessions, _ = fork_env

    def fail_copy(mapper, connection, target):
        if target.chat_id != "source" and target.chat_seq == 2:
            raise RuntimeError("injected copy failure")

    event.listen(ChatMessage, "before_insert", fail_copy)
    try:
        with pytest.raises(RuntimeError, match="injected copy failure"):
            fork(client)
    finally:
        event.remove(ChatMessage, "before_insert", fail_copy)
    with sessions() as db:
        assert db.query(ChatSession).count() == 1
        assert db.query(ChatMessage).count() == 4


def test_long_history_is_not_limited_to_ui_page(fork_env, monkeypatch):
    client, sessions, _ = fork_env
    with sessions() as db:
        db.add_all(
            [
                ChatMessage(
                    chat_id="source",
                    message_id=f"long_{i}",
                    chat_seq=i,
                    role="assistant" if i % 2 == 0 else "user",
                    content=f"item {i} " * 40,
                )
                for i in range(5, 122)
            ]
        )
        db.commit()
    response = fork(client)
    assert response.status_code == 201
    cid = response.json()["data"]["chat_id"]
    assert response.json()["data"]["message_count"] == 120
    with sessions() as db:
        assert db.query(ChatMessage).filter_by(chat_id=cid).count() == 120
        assert db.get(ChatSession, cid).next_message_seq == 121

    # The next model turn uses the normal compaction entry point on copied raw rows.
    import asyncio
    from dataclasses import replace

    from core.db import engine as engine_module
    from core.services import compaction_service as compaction

    monkeypatch.setattr(engine_module, "SessionLocal", sessions)
    monkeypatch.setattr(
        compaction,
        "settings",
        replace(
            compaction.settings,
            compaction=replace(compaction.settings.compaction, keep_recent_tokens=0),
        ),
    )

    async def summarize(history, *, timeout):
        assert "item 121" not in str(history)
        return "fork prefix summary"

    monkeypatch.setattr(compaction, "_summarize", summarize)
    with sessions() as db:
        history = load_session_history(ChatService(db), cid, "owner")
    history, compacted = asyncio.run(
        compaction.maybe_run_pre_turn_compaction(
            cid,
            history,
            model_name="test",
            context_window=1024,
            system_prompt="",
            tool_schema=[],
            provider_overhead_tokens=0,
        )
    )
    assert compacted is True
    assert "fork prefix summary" in str(history)
    with sessions() as db:
        assert db.get(ChatCompactionState, cid).covered_seq == 120
        assert db.get(ChatCompactionState, "source") is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"request_id": "invalid"},
        {"request_id": str(uuid4()), "title": "   "},
        {"request_id": str(uuid4()), "unexpected": True},
    ],
)
def test_validates_request_contract(fork_env, payload):
    client, _, _ = fork_env
    assert client.post("/v1/chats/source/fork", json=payload).status_code == 422


def test_distinct_requests_create_distinct_branches_and_append_independently(fork_env):
    client, sessions, _ = fork_env
    first = fork(client, through_message_id="a1").json()["data"]["chat_id"]
    second = fork(client, through_message_id="a1").json()["data"]["chat_id"]
    assert first != second
    with sessions() as db:
        db.add(
            ChatMessage(
                chat_id=first,
                message_id="branch_question",
                role="user",
                content="Continue from violet",
            )
        )
        db.commit()
        assert db.get(ChatMessage, "branch_question").chat_seq == 3
        assert db.query(ChatMessage).filter_by(chat_id=second).count() == 2
        assert db.query(ChatMessage).filter_by(chat_id="source").count() == 4


def test_same_request_cannot_reuse_other_source_or_deleted_branch(fork_env):
    from datetime import datetime, timezone

    client, sessions, _ = fork_env
    request_id = str(uuid4())
    cid = fork(client, request_id=request_id).json()["data"]["chat_id"]
    other = client.post(f"/v1/chats/{cid}/fork", json={"request_id": request_id})
    assert other.status_code == 409
    with sessions() as db:
        db.get(ChatSession, cid).deleted_at = datetime.now(timezone.utc)
        db.commit()
    response = fork(client, request_id=request_id)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "fork_deleted"


def test_next_turn_collects_inherited_attachment_context(fork_env, monkeypatch):
    from core.chat.context import collect_historical_attachments
    from core.db import engine as engine_module
    from core.db.models import Artifact

    client, sessions, _ = fork_env
    with sessions() as db:
        Artifact.__table__.create(db.get_bind())
        db.add(
            Artifact(
                artifact_id="f1",
                user_id="owner",
                chat_id="source",
                type="document",
                title="Notes",
                filename="notes.txt",
                size_bytes=12,
                mime_type="text/plain",
                storage_key="test/f1",
                summary="The chosen color is violet",
            )
        )
        db.commit()
    monkeypatch.setattr(engine_module, "SessionLocal", sessions)
    cid = fork(client, through_message_id="a1").json()["data"]["chat_id"]
    attachments = collect_historical_attachments(cid, "owner", set())
    assert len(attachments) == 1
    assert attachments[0]["file_id"] == "f1"
    assert attachments[0]["summary"] == "The chosen color is violet"
    denied = collect_historical_attachments(cid, "another-user", set())
    assert denied[0]["deleted"] is True
    assert "summary" not in denied[0]


def test_branching_does_not_change_usage_or_billing_reports(fork_env):
    from api.deps import require_config
    from api.routes.v1 import admin_billing, admin_usage_logs, me_logs
    from core.db.models import ModelPricing, UserShadow

    client, sessions, _ = fork_env
    client.app.dependency_overrides[require_config] = lambda: None
    for module in (admin_billing, admin_usage_logs, me_logs):
        client.app.include_router(module.router)
    with sessions() as db:
        for model in (UserShadow, ModelPricing):
            model.__table__.create(db.get_bind())
        db.add(UserShadow(user_id="owner", username="Owner"))
        db.commit()
    paths = [
        "/v1/me/logs/usage/summary",
        "/v1/admin/usage-logs/summary",
        "/v1/admin/billing/summary",
    ]
    before = [client.get(path) for path in paths]
    assert [r.status_code for r in before] == [200] * 3
    before_data = [r.json()["data"] for r in before]
    assert all(sum(row["total_requests"] for row in rows) == 2 for rows in before_data)
    assert all(sum(row["total_tokens"] for row in rows) == 15 for rows in before_data)
    assert fork(client).status_code == 201
    after_data = [client.get(path).json()["data"] for path in paths]
    assert after_data == before_data
    for path in ["/v1/me/logs/usage", "/v1/admin/usage-logs"]:
        result = client.get(path)
        assert result.status_code == 200
        assert len(result.json()["data"]["items"]) == 2
