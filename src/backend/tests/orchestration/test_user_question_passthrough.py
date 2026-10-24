"""ask_user_question 的提问必须穿过工作流层到达 SSE。

线上故障：工具挂起并登记了问题，但 workflow 的事件分发只放行 file_confirm /
design_pick，user_question 被静默丢弃 —— 前端收不到提问卡片，会话看起来就卡死在
「调用了询问用户工具」那一步，直到两小时超时。
"""

from __future__ import annotations
from core.agent_runtime.integrations import memory_identity as memory_identity_module
from core.agent_runtime.integrations import memory_session as memory_session_module
from core.llm.runtime import streaming as streaming_module
from core.llm.citations import allocator as allocator_module

from types import SimpleNamespace

import core.agent_runtime.integrations.memory as memory
import core.agent_runtime.runner as runner
import core.agent_runtime.targets as targets
import core.capabilities.catalog.resolver as catalog_resolver
import core.llm.citations.anchor as citation_anchor
import core.llm.factory as factory
import core.llm.runtime.streaming as streaming
import pytest
from core.agent_runtime.integrations.memory_session import SessionMemory


@pytest.mark.asyncio
async def test_user_question_events_reach_the_stream(monkeypatch):
    from core.db import engine as db_engine
    from core.capabilities.agents import builtin as builtin_subagents
    from core.chat.compaction import service as compaction_service
    from core.capabilities.agents import edition as user_agent_service
    from core.auth import users as user_service
    pass

    class DummySession:
        def __enter__(self):
            return SimpleNamespace()

        def __exit__(self, *_args):
            return False

    class FakeStreamingAgent:
        def __init__(self, agent, _clients):
            self.agent = agent

        async def stream(self, _messages, _context):
            yield "user_question", {
                "request_id": "req-1",
                "questions": [{"id": "q1", "question": "A 还是 B？"}],
            }
            yield "user_question_resolved", {"request_id": "req-1", "outcome": "answered"}

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
        return SessionMemory(chat_id="", scope_user_id="", workspace_id="default", memory_enabled=False)

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
    monkeypatch.setattr(factory, "create_agent_executor", create_agent)
    monkeypatch.setattr(memory_session_module, 'open_session_memory', no_memory)
    monkeypatch.setattr(memory_identity_module, 'build_user_identity_block', no_identity)
    monkeypatch.setattr(allocator_module, 'anchor_start_for_chat', lambda _chat_id: 0)
    monkeypatch.setattr(catalog_resolver, "enabled_skill_ids_from_context", lambda _ctx: [])
    monkeypatch.setattr(catalog_resolver, "enabled_mcp_ids_from_context", lambda _ctx: [])
    monkeypatch.setattr(catalog_resolver, "enabled_kb_ids_from_context", lambda _ctx: [])
    monkeypatch.setattr(targets, "_resolve_mode_spec", lambda _ctx: None)
    monkeypatch.setattr(streaming_module, 'StreamingAgent', FakeStreamingAgent)

    chunks = [
        chunk
        async for chunk in runner.astream_chat_workflow(
            session_messages=[{"role": "user", "content": "问我一个问题"}],
            user_message="问我一个问题",
            context={
                "run_id": "run-user-question",
                "journal_owner": "worker-user-question",
                "chat_id": "chat-user-question",
                "user_id": "user-user-question",
                "memory_enabled": False,
                "ontology_runtime": {},
            },
        )
    ]

    asked = [chunk for chunk in chunks if chunk.get("type") == "user_question"]
    resolved = [chunk for chunk in chunks if chunk.get("type") == "user_question_resolved"]
    assert [chunk["request_id"] for chunk in asked] == ["req-1"]
    assert asked[0]["questions"][0]["question"] == "A 还是 B？"
    assert [chunk["outcome"] for chunk in resolved] == ["answered"]
