"""Context projection regressions through the authenticated history API."""

from datetime import datetime, timezone
from types import SimpleNamespace

from api.routes.v1.chats.history import get_context_usage
from core.llm.context.usage import build_compaction_context_usage, build_context_usage_snapshot
from core.chat.service import ChatService
from core.db.models import ChatSession


def _setup(db):
    db.add(ChatSession(chat_id="usage-audit", user_id="u1", title="audit"))
    db.commit()
    svc = ChatService(db)
    agent = SimpleNamespace(
        model=SimpleNamespace(model="demo", context_size=1000),
        state=SimpleNamespace(model_provider_id="p1"),
        request_context_manifest={},
    )
    usage = build_context_usage_snapshot(
        agent, prompt_tokens=700, completion_tokens=50, model_call_index=1
    )
    row = svc.message_repo.create(
        dict(
            message_id="answer",
            chat_id="usage-audit",
            role="assistant",
            content="old answer",
            extra_data={"context_usage": usage},
        )
    )
    compacted = build_compaction_context_usage(
        {
            "system_prompt_tokens": 40,
            "tool_schema_tokens": 20,
            "message_tokens": 15,
            "provider_overhead_tokens": 7,
        },
        context_window=1000,
        model_name="demo",
        model_provider_id="p1",
    )
    ckpt = svc.add_compaction_checkpoint(
        "usage-audit",
        summary_text="summary",
        replacement_history=[{"role": "user", "content": "summary"}],
        covered_seq=row.chat_seq,
        replacement_manifest={"replacement_context_usage": compacted},
    )
    return svc, row, ckpt, usage


def _read(db):
    return get_context_usage("usage-audit", user=SimpleNamespace(user_id="u1"), db=db)["data"]


def test_context_endpoint_returns_compressed_total_after_checkpoint(db_session):
    _setup(db_session)
    data = _read(db_session)
    assert data["context_usage"]["used_tokens"] == 82
    assert data["context_usage"]["source"] == "compaction_estimate"


def test_same_assistant_row_measured_after_midturn_checkpoint_supersedes_it(db_session):
    svc, row, ckpt, usage = _setup(db_session)
    ckpt.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    usage = {**usage, "captured_at_ms": 1767225601000}
    svc.update_message_extra_data(row.message_id, {"context_usage": usage})
    db_session.commit()
    data = _read(db_session)
    assert data["context_usage"]["used_tokens"] == 750
    assert "context_usage" not in data["context_compaction"]


def test_midturn_checkpoint_counts_the_active_system_and_tool_surface(db_session, monkeypatch):
    import asyncio
    import dataclasses
    from agentscope.agent import ContextConfig
    from agentscope.message import Msg, TextBlock
    from sqlalchemy.orm import sessionmaker
    import core.db.engine as engine
    from core.llm.context.compacting_agent import CompactingAgent
    from core.config import settings as config_settings
    from core.chat.compaction import service as service

    svc, row, ckpt, usage = _setup(db_session)
    svc.message_repo.create(
        dict(message_id="new-input", chat_id="usage-audit", role="user", content="new input")
    )
    monkeypatch.setattr(engine, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr(
        config_settings,
        "settings",
        dataclasses.replace(
            config_settings.settings,
            compaction=dataclasses.replace(config_settings.settings.compaction, keep_recent_tokens=1),
        ),
    )

    async def summarize(history, **kwargs):
        return "short summary"

    monkeypatch.setattr('core.chat.compaction.summary._summarize', summarize)
    agent = object.__new__(CompactingAgent)
    agent._jx_observation = None
    agent._jx_compacted_cursor = None
    agent._jx_trigger_ratio = 0.8
    agent.model = SimpleNamespace(context_size=1000, model="demo")
    agent.offloader = None
    agent.context_config = ContextConfig()
    agent.state = SimpleNamespace(
        context=[
            Msg(name="user", role="user", content=[TextBlock(text="old " * 200)]),
            Msg(name="assistant", role="assistant", content=[TextBlock(text="tail")]),
        ],
        summary="",
        chat_id="usage-audit",
        session_id="s",
        model_provider_id="p1",
    )
    agent._jx_compaction_system_prompt = "S" * 400
    agent._jx_compaction_tool_schemas = [{"name": "search", "description": "T" * 80}]
    agent.observe_context_tokens(900, 10)
    asyncio.run(agent.compress_context())
    db_session.expire_all()
    data = _read(db_session)
    compressed = data["context_usage"]
    assert compressed["source"] == "compaction_estimate"
    assert compressed["context_window"] == 1000
    assert compressed["model_provider_id"] == "p1"
    # The exact frozen system prompt alone is 100 estimated tokens.
    assert compressed["breakdown"]["system"] == 143


def test_context_projection_uses_one_checkpoint_revision(db_session, monkeypatch):
    svc, row, checkpoint, usage = _setup(db_session)
    original = ChatService.get_latest_compaction_checkpoint
    reads = []

    def read_once(self, chat_id):
        reads.append(chat_id)
        assert len(reads) == 1, "one response must not combine successive checkpoints"
        return original(self, chat_id)

    monkeypatch.setattr(ChatService, "get_latest_compaction_checkpoint", read_once)
    data = _read(db_session)
    assert data["context_usage"]["checkpoint_id"] == data["context_compaction"]["checkpoint_id"]
    assert data["context_usage"]["used_tokens"] == 82
