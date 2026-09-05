from types import SimpleNamespace

from core.llm.subagent_tool import _run_subagent_in_thread
from core.llm.tools._common import resolve_sandbox_session


def test_empty_override_falls_back_to_chat_session():
    assert resolve_sandbox_session(None, "chat-1") == "chat-1"
    assert resolve_sandbox_session("", "chat-1") == "chat-1"
    assert resolve_sandbox_session("explicit", "chat-1") == "explicit"


def test_custom_subagent_reuses_parent_session(monkeypatch):
    captured = {}

    class FakeDbContext:
        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return None

    class FakeUserAgentService:
        def __init__(self, db):
            del db

        def get_raw_by_id(self, agent_id, *, user_id):
            assert agent_id == "custom-1"
            assert user_id == "user-1"
            return SimpleNamespace(
                mcp_server_ids=[],
                skill_ids=[],
                kb_ids=[],
                system_prompt="custom",
                model_provider_id=None,
                max_iters=3,
                temperature=0.1,
                max_tokens=100,
                timeout=30,
            )

    class FakeReply:
        def get_text_content(self):
            return "done"

    class FakeAgent:
        state = SimpleNamespace(context=[])

        async def reply(self, *, inputs):
            assert inputs is not None
            return FakeReply()

    async def fake_create_agent_executor(**kwargs):
        captured.update(kwargs)
        return FakeAgent(), []

    async def no_op(*args, **kwargs):
        del args, kwargs

    monkeypatch.setattr("core.db.engine.SessionLocal", lambda: FakeDbContext())
    monkeypatch.setattr(
        "core.services.user_agent_service.UserAgentService",
        FakeUserAgentService,
    )
    monkeypatch.setattr("core.llm.builtin_subagents.get_builtin_subagent", lambda _id: None)
    monkeypatch.setattr(
        "core.llm.agent_factory.create_agent_executor",
        fake_create_agent_executor,
    )
    monkeypatch.setattr("core.llm.mcp_manager.close_clients", no_op)
    monkeypatch.setattr("core.infra.redis.close_redis", no_op)
    ok, text, _pinned, _messages = _run_subagent_in_thread(
        "custom-1",
        "Custom",
        "do work",
        "",
        "user-1",
        parent_runtime={
            "sandbox_session_id": "chat-sandbox-1",
            "chat_id": "chat-1",
        },
    )

    assert ok is True
    assert text == "done"
    assert captured["sandbox_session_id"] == "chat-sandbox-1"
