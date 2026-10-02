"""Behavioral regression coverage."""

from __future__ import annotations

import api.routes.v1.chats.run_views as chat_run_views
import fakeredis.aioredis
import pytest
from agentscope.agent import Agent, ReActConfig
from agentscope.exception import DeveloperOrientedException
from agentscope.message import ToolCallBlock, UserMsg
from agentscope.model import ChatResponse, ChatUsage
from agentscope.permission import PermissionContext, PermissionMode
from agentscope.tool import FunctionTool, Toolkit
from api.routes.v1 import chats as chat_routes
from core.auth.backend import UserContext
from core.db.models import ChatRun, ChatSession
from core.llm.middlewares import AgentRuntimeState, ToolEffectMiddleware
from core.services.run_journal import RunJournal
from core.services.tool_effect_ledger import ToolEffectJournal, recover_incomplete_tool_effects
from orchestration import chat_run_executor as executor
from tests.orchestration.recovery_test_support import recovery_env


@pytest.mark.asyncio
async def test_start_run_commits_acceptance_and_snapshot_before_registering_worker(
    recovery_env, monkeypatch
):
    sessions = recovery_env
    observed = {}

    def register(run_id, coro, *, name):
        with sessions() as db:
            row = db.get(ChatRun, run_id)
            observed.update(
                exists=row is not None,
                status=row.status,
                phase=row.run_phase,
                snapshot=dict(row.recovery_snapshot or {}),
                name=name,
            )
        coro.close()

    monkeypatch.setattr(executor, "_register_run_task", register)

    run = await executor.start_run(
        chat_id="chat-1",
        user_id="user-1",
        session_messages=[{"role": "user", "content": "hello"}],
        effective_user_message="hello with context",
        raw_user_message="hello",
        context={"user_id": "user-1", "enabled_mcp_ids": []},
        request_payload={"kind": "chat", "message": "hello"},
        model_name="test-model",
    )

    assert observed["exists"] is True
    assert observed["status"] == "pending"
    assert observed["phase"] == "accepted"
    assert observed["snapshot"]["kind"] == "chat"
    args = observed["snapshot"]["worker_args"]
    assert args["session_messages"][-1]["content"] == "hello"
    assert args["effective_user_message"] == "hello with context"
    assert args["model_name"] == "test-model"
    assert run.run_id.startswith("run_")


def test_active_run_probe_hides_internal_agent_rows(recovery_env):
    sessions = recovery_env
    journal = RunJournal(sessions)
    internal = journal.accept(
        run_id="run-internal-hidden",
        message_id="msg-internal-hidden",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "internal_job_agent"},
        recovery_snapshot={"kind": "internal_job_agent"},
    )
    assert journal.claim(internal.run_id, owner="internal", lease_seconds=60)

    assert executor.get_active_run_for_chat("chat-1", "user-1") is None

    public = journal.accept(
        run_id="run-public-visible",
        message_id="msg-public-visible",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )
    assert journal.claim(public.run_id, owner="public", lease_seconds=60)
    assert executor.get_active_run_for_chat("chat-1", "user-1").run_id == public.run_id


def test_list_active_runs_spans_chats_and_hides_background_kinds(recovery_env):
    sessions = recovery_env
    with sessions() as db:
        db.add(ChatSession(chat_id="chat-2", user_id="user-1", title="second"))
        db.add(ChatSession(chat_id="chat-3", user_id="user-2", title="other user"))
        db.commit()
    journal = RunJournal(sessions)

    live_one = journal.accept(
        run_id="run-live-1",
        message_id="msg-live-1",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )
    live_two = journal.accept(
        run_id="run-live-2",
        message_id="msg-live-2",
        chat_id="chat-2",
        user_id="user-1",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )
    journal.accept(
        run_id="run-batch",
        message_id="msg-batch",
        chat_id="chat-2",
        user_id="user-1",
        request_payload={"kind": "batch_item"},
        recovery_snapshot={"kind": "batch_item"},
    )
    journal.accept(
        run_id="run-other-user",
        message_id="msg-other-user",
        chat_id="chat-3",
        user_id="user-2",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )

    listed = executor.list_active_runs_for_user("user-1")
    assert {row.chat_id for row in listed} == {"chat-1", "chat-2"}
    assert {row.run_id for row in listed} == {live_one.run_id, live_two.run_id}

    # A finished run must stop lighting the sidebar dot, even while its writer
    # slot lingers — the listing is "still running", not "was running".
    with sessions() as db:
        row = db.get(ChatRun, live_one.run_id)
        row.status = "completed"
        db.commit()
    assert {row.chat_id for row in executor.list_active_runs_for_user("user-1")} == {"chat-2"}


def test_active_runs_route_returns_chat_ids_and_outranks_the_detail_route(recovery_env):
    sessions = recovery_env
    journal = RunJournal(sessions)
    live = journal.accept(
        run_id="run-route-live",
        message_id="msg-route-live",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )

    with sessions() as db:
        response = chat_run_views.list_active_chat_runs(
            user=UserContext(
                user_id="user-1",
                user_center_id="center-1",
                username="tester",
            ),
            db=db,
        )

    assert response["data"]["items"] == [
        {
            "chat_id": "chat-1",
            "run_id": live.run_id,
            "status": "pending",
            "started_at": None,
        }
    ]

    # "/active-runs" must be declared before "/{chat_id}", otherwise the detail
    # route swallows it and the sidebar probe 404s on a chat named active-runs.
    paths = [route.path for route in chat_routes.router.routes]
    assert paths.index("/v1/chats/active-runs") < paths.index("/v1/chats/{chat_id}")


@pytest.mark.asyncio
async def test_public_worker_keeps_ambiguous_agent_tool_call_recoverable(recovery_env, monkeypatch):
    sessions = recovery_env
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("orchestration.run_event_stream.get_redis", lambda **_: redis)
    journal = RunJournal(sessions)
    row = journal.accept(
        run_id="run-agent-timeout",
        message_id="msg-agent-timeout",
        chat_id="chat-1",
        user_id="user-1",
        request_payload={"kind": "chat"},
        recovery_snapshot={"kind": "chat", "worker_args": {}},
    )

    async def exploding_probe(value: int):
        """Simulate an adapter that never produces a ToolResponse."""
        del value
        raise DeveloperOrientedException("adapter response was lost")

    class ToolModel:
        model = "ambiguous-tool"
        context_size = 32768

        async def __call__(self, messages, tools=None, **_kwargs):
            del messages, tools
            return ChatResponse(
                content=[
                    ToolCallBlock(
                        id="ambiguous-provider-call",
                        name="exploding_probe",
                        input='{"value":1}',
                    )
                ],
                is_last=False,
                usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.01),
            )

        async def count_tokens(self, messages, tools=None):
            del messages, tools
            return 1

    async def workflow(*, context, **_kwargs):
        agent = Agent(
            name="worker-public-seam",
            system_prompt="Call the tool.",
            model=ToolModel(),
            toolkit=Toolkit(tools=[FunctionTool(exploding_probe)]),
            middlewares=[ToolEffectMiddleware(session_factory=sessions)],
            state=AgentRuntimeState(
                run_id=context["run_id"],
                journal_owner=context["journal_owner"],
                permission_context=PermissionContext(mode=PermissionMode.BYPASS),
            ),
            react_config=ReActConfig(max_iters=1),
        )
        await agent.reply(UserMsg(name="user", content="run"))
        if False:
            yield {}

    monkeypatch.setattr(executor, "astream_chat_workflow", workflow)
    await executor._run_workflow(
        run_id=row.run_id,
        chat_id=row.chat_id,
        user_id=row.user_id,
        message_id=row.message_id,
        session_messages=[{"role": "user", "content": "run"}],
        effective_user_message="run",
        raw_user_message="run",
        context={"user_id": row.user_id},
        model_name="test-model",
    )

    with sessions() as db:
        paused = db.get(ChatRun, row.run_id)
        assert paused.status == "needs_attention"
        assert paused.run_phase == "needs_attention"
    decisions = await recover_incomplete_tool_effects(journal=ToolEffectJournal(sessions))
    assert [item.action for item in decisions] == ["needs_attention"]
