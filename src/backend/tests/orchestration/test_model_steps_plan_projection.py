"""The step record travels hook → stream → executor → row → replay unchanged.

The hook adapter records each completed model response, the streaming adapter
emits it (and every tool result as saved into context) as ``model_step``
events, the executor persists the list with the assistant row, and history
loading replays it. A run stopped mid-tool closes its dangling call.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from agentscope.message import Msg, ToolResultBlock
from core.db.models import ChatMessage
from core.services.compaction_history import normalize_rows
from orchestration import chat_run_executor as executor
from orchestration.memory_integration import SessionMemory
from tests.orchestration.test_model_steps_pipeline import _fake, _run, _step, _stream_events
from tests.orchestration.test_model_steps_pipeline import run_env as run_env


@pytest.mark.asyncio
@pytest.mark.parametrize("persist_result", [True, False])
async def test_plan_bar_and_canonical_results_survive_workflow_and_persistence(
    run_env, monkeypatch, persist_result
):
    from core.db import engine as db_engine
    from core.llm import builtin_subagents
    from core.services import compaction_service, user_agent_service, user_service
    from orchestration import workflow

    class DummySession:
        def __enter__(self):
            return SimpleNamespace()

        def __exit__(self, *_args):
            return False

    class FakeStreamingAgent:
        def __init__(self, agent, _clients):
            self.agent = agent

        async def stream(self, _messages, _context):
            plan = {
                "title": "Plan",
                "steps": [{"title": "Read", "status": "in_progress"}],
            }
            result = ToolResultBlock(
                type="tool_result",
                id="plan-1",
                name="update_plan",
                output="Plan updated: 1 in progress",
                state="success",
            )
            state = SimpleNamespace(
                user_id="",
                chat_id="",
                run_id="",
                tool_effect_links={},
                apply_request_context=lambda ctx, text: None,
                context=[Msg(name="assistant", role="assistant", content=[result])],
                pending_model_steps=[
                    _step(
                        {
                            "type": "tool_call",
                            "id": "plan-1",
                            "name": "update_plan",
                            "input": "{}",
                        }
                    )["step"]
                ],
            )
            events = [
                _fake("ModelCallEndEvent", input_tokens=1, output_tokens=1),
                _fake(
                    "ToolCallStartEvent",
                    tool_call_id="plan-1",
                    tool_call_name="update_plan",
                ),
                _fake("ToolCallDeltaEvent", tool_call_id="plan-1", delta=json.dumps(plan)),
                _fake("ToolCallEndEvent", tool_call_id="plan-1"),
                _fake(
                    "ToolResultEndEvent",
                    tool_call_id="plan-1",
                    tool_call_name="update_plan",
                    state="success",
                ),
            ]
            for item in await _stream_events(events, state):
                yield item

        async def aget_usage(self):
            return {}

        def get_context_usage(self, _usage):
            return None

        async def shutdown(self):
            return None

    async def create_agent(**_kwargs):
        return (
            SimpleNamespace(
                model=SimpleNamespace(model="test-model", context_size=32_768),
                state=SimpleNamespace(ontology_runtime={}),
            ),
            [],
        )

    async def no_memory(*_args, **_kwargs):
        return SessionMemory(
            chat_id="", scope_user_id="", workspace_id="default", memory_enabled=False
        )

    async def no_identity(_user_id):
        return ""

    async def no_compaction(_chat_id, messages, **_kwargs):
        return messages, None

    monkeypatch.setattr(db_engine, "SessionLocal", lambda: DummySession())
    monkeypatch.setattr(
        user_agent_service,
        "UserAgentService",
        lambda _db: SimpleNamespace(list_for_user=lambda _user_id: []),
    )
    monkeypatch.setattr(
        user_service,
        "UserService",
        lambda _db: SimpleNamespace(get_disabled_builtin_subagent_ids=lambda _user_id: set()),
    )
    monkeypatch.setattr(builtin_subagents, "merge_builtin_subagents", lambda *_a, **_kw: [])
    monkeypatch.setattr(compaction_service, "maybe_run_pre_turn_compaction", no_compaction)
    monkeypatch.setattr(workflow, "create_agent_executor", create_agent)
    monkeypatch.setattr(workflow, "open_session_memory", no_memory)
    monkeypatch.setattr(workflow, "build_user_identity_block", no_identity)
    monkeypatch.setattr(workflow, "anchor_start_for_chat", lambda _chat_id: 0)
    monkeypatch.setattr(workflow, "enabled_skill_ids_from_context", lambda _ctx: [])
    monkeypatch.setattr(workflow, "enabled_mcp_ids_from_context", lambda _ctx: [])
    monkeypatch.setattr(workflow, "enabled_kb_ids_from_context", lambda _ctx: [])
    monkeypatch.setattr(workflow, "_resolve_mode_spec", lambda _ctx: None)
    monkeypatch.setattr(workflow, "StreamingAgent", FakeStreamingAgent)
    monkeypatch.setattr(workflow, "_persistent_clients", [])

    if not persist_result:
        original_refresh = executor.ChatService.refresh_streaming_message

        def fail_result(self, **kwargs):
            if any(s["kind"] == "tool_result" for s in kwargs.get("model_steps", [])):
                raise RuntimeError("injected result persistence failure")
            return original_refresh(self, **kwargs)

        monkeypatch.setattr(executor.ChatService, "refresh_streaming_message", fail_result)
    saved_plans = []
    monkeypatch.setattr(
        workflow, "_save_plan_progress", lambda chat, plan: saved_plans.append(plan)
    )
    published = []
    original_emit = executor._xadd_event

    async def emit(run_id, offset, event):
        published.append(event)
        await original_emit(run_id, offset, event)

    monkeypatch.setattr(executor, "_xadd_event", emit)
    run = await _run(workflow.astream_chat_workflow, monkeypatch)
    if not persist_result:
        assert not saved_plans
        assert not any(event["type"] == "plan_update" for event in published)
        assert any(event["type"] == "error" for event in published)
        return
    with run_env() as db:
        row = db.get(ChatMessage, run.message_id)
        replay = normalize_rows([row])
        assert [step["kind"] for step in row.model_steps] == [
            "assistant",
            "tool_result",
        ]
        assert row.model_steps[1]["blocks"][0]["output"] == "Plan updated: 1 in progress"
    assert [row["role"] for row in replay] == ["assistant", "tool"]
    assert saved_plans and saved_plans[0]["title"] == "Plan"
    assert any(event["type"] == "plan_update" for event in published)
    assert not any(
        event["type"] in ("tool_call", "tool_result") and event.get("tool_name") == "update_plan"
        for event in published
    )
