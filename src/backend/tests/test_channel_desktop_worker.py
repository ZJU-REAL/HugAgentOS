"""Local runtime integration; model/network boundaries use deterministic fixtures."""

import asyncio
from dataclasses import asdict

import fakeredis.aioredis
import pytest
from core.channels.protocol import ChannelCaps, InboundMsg, SendResult
from core.db.engine import Base
from core.db.models import (
    ChannelConnection,
    ChatMessage,
    ChatRun,
    ChatSession,
    ContentBlock,
    UserShadow,
)
from core.services import channel_desktop_worker as worker
from core.services.run_journal import RunJournal
from orchestration import chat_run_executor as executor
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'local.sqlite'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        db.add(UserShadow(user_id="local-owner", username="owner", extra_data={}))
        db.commit()
    for target in (
        "core.db.engine.SessionLocal",
        "core.channels.inbound.SessionLocal",
        "orchestration.chat_run_executor.SessionLocal",
        "orchestration.tool_effect_recovery.SessionLocal",
    ):
        monkeypatch.setattr(target, sessions)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("orchestration.run_event_stream.get_redis", lambda **_: redis)
    monkeypatch.setattr(executor, "_spawn_followup_task", lambda **_: None)
    monkeypatch.setattr(executor, "_spawn_compaction_task", lambda **_: None)
    monkeypatch.setattr("core.services.artifact_service.persist_artifacts", lambda *a, **kw: None)
    executor._active_runs.clear()
    yield sessions
    executor._active_runs.clear()
    engine.dispose()


async def test_local_delivery_uses_default_chat_and_preserves_owner(runtime, tmp_path, monkeypatch):
    sessions = runtime
    identity = {"cloud_base": "https://cloud.invalid", "subject": "account", "device_id": "pc"}
    captured = dict(identity)
    monkeypatch.setattr(worker.bridge, "require_current_account", lambda _: None)
    monkeypatch.setattr("core.services.channel_relay_dispatch.bridge_enabled", lambda: True)
    with sessions() as db:
        db.add(
            ContentBlock(
                id=worker.GRANT_PREFIX + "grant", payload={**identity, "local_owner": "local-owner"}
            )
        )
        db.commit()
    item = {
        "binding_id": "grant",
        "delivery_id": "delivery",
        "lease": "a" * 32,
        "caps": asdict(ChannelCaps(channel_type="fixture")),
        "bot": {
            "channel_type": "fixture",
            "display_name": "Local bot",
            "agent_id": None,
            "group_listen_mode": "mention_only",
        },
        "message": asdict(
            InboundMsg(
                channel_id="cloud-bot",
                channel_type="fixture",
                text="read fixture",
                chat_type="p2p",
                external_conversation_id="peer",
                message_id="worker-message",
            )
        ),
    }
    fixture_file = tmp_path / "local-content.txt"
    fixture_file.write_text("content from this machine")
    seen = {}

    async def workflow(**kwargs):
        seen.update(kwargs)
        yield {"type": "content", "delta": fixture_file.read_text()}
        yield {"type": "meta", "route": "main", "usage": {"input_tokens": 1, "output_tokens": 1}}

    monkeypatch.setattr(executor, "astream_chat_workflow", workflow)
    sent = []

    async def cloud(_captured, path, payload):
        if path.endswith("/renew"):
            return {"ok": True}
        assert path == "delivery/operation"
        assert payload["lease"] == item["lease"]
        assert payload["method"] in {"send_placeholder", "send_text", "send_markdown"}
        sent.append(payload["args"]["text"])
        return asdict(SendResult.ok())

    monkeypatch.setattr(worker, "cloud_post", cloud)
    assert (
        await asyncio.wait_for(
            worker.run_delivery(sessions, captured, identity, "local-owner", item), 10
        )
        is None
    )
    assert "content from this machine" in sent
    assert seen["context"]["user_id"] == "local-owner"
    assert not seen["context"].get("project_ctx")
    with sessions() as db:
        chat = db.scalar(select(ChatSession))
        run = db.scalar(select(ChatRun))
        assert chat.project_id is None
        assert chat.user_id == "local-owner" and chat.channel_id != "cloud-bot"
        assert run.request_payload["source"] == "desktop_channel"
        assert run.status == "completed"
        assert db.get(ChatMessage, run.message_id).content == "content from this machine"
        conn = db.get(ChannelConnection, chat.channel_id)
        assert "app_secret" not in conn.config
    from core.infra.exceptions import BadRequestError

    with pytest.raises(BadRequestError, match="不重复执行"):
        await worker.run_delivery(sessions, captured, identity, "local-owner", item)


async def test_restart_cancels_local_run_before_recovery(runtime, monkeypatch):
    sessions = runtime
    with sessions() as db:
        db.add(ChatSession(chat_id="restart-chat", user_id="local-owner", title="Restart"))
        db.commit()
    journal = RunJournal(sessions)
    journal.accept(
        run_id="local-run",
        message_id="local-msg",
        chat_id="restart-chat",
        user_id="local-owner",
        request_payload={"kind": "chat", "source": "desktop_channel"},
        recovery_snapshot={"kind": "chat", "worker_args": {"context": {"user_id": "local-owner"}}},
    )
    assert await executor.recover_orphan_runs() == 1
    with sessions() as db:
        assert db.get(ChatRun, "local-run").status == "cancelled"
    assert "local-run" not in executor._active_runs
