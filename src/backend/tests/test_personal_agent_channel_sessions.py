"""One Hugo identity, independent peer/room histories and source-routed results."""
import pytest
from types import SimpleNamespace
from sqlalchemy import select
from integrations.channels.protocol import InboundMsg
from integrations.channels.models import ChannelConnection
from core.db.models import ChatMessage
from personal_agent.models import PersonalAgent, PersonalMessageInbox, PersonalWorkItem
from personal_agent.context import AgentContext
from personal_agent.identity.config import locked_agent
from personal_agent.conversation.message_routing import admit
from personal_agent.conversation.conversations import channel_session, listing
from tests.test_personal_agent_routing import sessions, personal_agent_on, turn, scripted  # noqa: F401
from tests.db_support import share_factory


def conversation(db, peer="alice", bot="bot", kind="p2p"):
    svc = AgentContext(db, "owner")
    svc.identity.ensure()
    conn = db.get(ChannelConnection, bot)
    if conn is None:
        conn = ChannelConnection(channel_id=bot, owner_user_id="owner", channel_type="lark",
            app_id=bot, display_name=bot, config={}, transport="long_conn", enabled=True)
        db.add(conn)
        svc.identity.agent().config = {**svc.identity.agent().config, "channel_ids": [*(svc.identity.agent().config or {}).get("channel_ids", []), bot]}
        db.commit()
    msg = InboundMsg(channel_id=bot, channel_type="lark", text="hello",
        external_conversation_id=peer, chat_type=kind, sender_name=peer)
    row = channel_session(db, locked_agent(db, "owner"), conn, msg)
    db.commit()
    return row.chat_id


def test_peer_room_bot_and_owner_boundaries(sessions):
    with sessions() as db:
        ids = [conversation(db, "same", "bot", "p2p"), conversation(db, "same", "bot", "group"),
               conversation(db, "bob"), conversation(db, "same", "bot2")]
        assert len(set(ids)) == 4
        assert conversation(db, "same") == ids[0]
        assert len(db.scalars(select(PersonalAgent)).all()) == 1
        svc = AgentContext(db, "owner", chat_id=ids[0])
        with pytest.raises(LookupError):
            AgentContext(db, "other", chat_id=ids[0]).conversation.list()
        assert svc.identity.agent().chat_id not in ids
        from core.db.repository.chat import ChatSessionRepository
        assert ChatSessionRepository(db).list_by_user("owner")[1] == 0
        with pytest.raises(ValueError): ChatSessionRepository(db).soft_delete(ids[0])


async def test_channel_history_reply_and_task_result_stay_in_source(sessions):
    with sessions() as db:
        alice = conversation(db)
        bob = conversation(db, "bob")
        admit(AgentContext(db, "owner"), "web private secret", "web")
    await turn(execute=scripted("web response"))
    with sessions() as db:
        admit(AgentContext(db, "owner", chat_id=bob), "bob private secret", "bob")
    await turn(execute=scripted("bob response"))
    captured = []
    async def inspect(plan):
        captured.extend(plan["history"])
        assert plan["chat_id"] == alice
    with sessions() as db:
        svc = AgentContext(db, "owner", chat_id=alice)
        admitted = admit(svc, "alice request", "alice")
    await turn(execute=scripted(inspect, ("pa_task_create", {"goal": "alice report"}), "started"))
    assert "web private secret" not in str(captured)
    assert "bob private secret" not in str(captured)
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = db.scalar(select(PersonalWorkItem))
        assert task.checkpoint["source_chat_id"] == alice
        svc.tasks.complete(task.id, "alice report done", {})
    await turn(execute=scripted("alice report complete"))
    with sessions() as db:
        svc = AgentContext(db, "owner")
        assert [row["content"] for row in svc.conversation.list()] == ["web private secret", "web response"]
        alice_rows = AgentContext(db, "owner", chat_id=alice).conversation.list()
        assert alice_rows[-1]["content"] == "alice report complete"
        assert len(listing(svc)) == 2
        assert db.get(ChatMessage, admitted["message_id"]).chat_id == alice


def test_http_conversation_scope_and_owner_checks(sessions, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.auth.backend import UserContext, get_current_user
    from core.db.engine import get_db
    from personal_agent.api.agent import router

    with sessions() as db:
        alice = conversation(db)
    app = FastAPI()
    app.include_router(router)
    def database():
        with sessions() as db: yield db
    app.dependency_overrides[get_db] = database
    share_factory(monkeypatch, sessions)
    app.dependency_overrides[get_current_user] = lambda: UserContext(user_id="owner", user_center_id="owner", username="owner")
    with TestClient(app) as client:
        response = client.post("/v1/personal-agent/messages", params={"chat_id": alice},
                               json={"content": "reply to Alice", "request_key": "web-reply"})
        assert response.status_code == 202
        ident = response.json()["data"]["message_id"]
        assert client.get("/v1/personal-agent/messages").json()["data"] == []
        assert client.get("/v1/personal-agent/messages", params={"chat_id": alice}).json()["data"][0]["id"] == ident
        assert len(client.get("/v1/personal-agent/conversations").json()["data"]) == 1
        app.dependency_overrides[get_current_user] = lambda: UserContext(user_id="other", user_center_id="other", username="other")
        assert client.get("/v1/personal-agent/messages", params={"chat_id": alice}).status_code == 404
        assert client.post("/v1/personal-agent/messages", params={"chat_id": alice},
                           json={"content":"foreign", "request_key":"foreign"}).status_code == 404


async def test_channel_delivery_uses_source_session_for_file_and_message(sessions, monkeypatch):
    import time
    from core.db.models import Artifact
    from personal_agent.delivery import delivery
    from personal_agent.channels import channel_bridge
    from personal_agent.tasks.task_ops import create
    with sessions() as db:
        alice = conversation(db)
        svc = AgentContext(db, "owner", chat_id=alice)
        from personal_agent.conversation.conversations import latest_origin
        entry = admit(svc, "make report", "report", extra={"channel_origin": latest_origin(db, svc.identity.agent(), alice)})
        message = db.get(ChatMessage, entry["message_id"])
        task = create(svc, message, "make report", "report-task")
        task.status, task.owner, task.run_id, task.lease_until = "running", "worker", "run", time.time()+300
        task_id, generation = task.id, svc.identity.agent().generation
        db.commit()
    class Storage:
        def upload_bytes(self, content, key): return key
        def delete(self, key): pass
    monkeypatch.setattr(delivery, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    monkeypatch.setattr('core.files.storage.get_storage', lambda: Storage())
    state={"owner":"worker","run_id":"run","generation":generation}
    receipt = delivery._publish(task_id, "owner", state, "delivery-id", "artifact-id", "hash",
                               b"report", "report.txt", "text/plain", "finished")
    with sessions() as db:
        item = db.get(Artifact, receipt["artifact_id"])
        message = db.get(ChatMessage, receipt["message_id"])
        assert item.chat_id == message.chat_id == alice
        assert message.extra_data["channel_delivery"]["origin"]["msg"]["external_conversation_id"] == "alice"
        assert channel_bridge.exported_artifact(db, alice, item.artifact_id) is not None
        assert len(AgentContext(db, "owner").conversation.list()) == 0


def test_session_idempotency_and_task_visibility(sessions):
    with sessions() as db:
        alice = conversation(db)
        bob = conversation(db, "bob")
        a = AgentContext(db, "owner", chat_id=alice)
        b = AgentContext(db, "owner", chat_id=bob)
        first = admit(a, "alice task", "same-key", intent="new")
        assert admit(a, "alice task", "same-key", intent="new") == first
        second = admit(b, "bob task", "same-key", intent="new")
        assert first["message_id"] != second["message_id"]
        task = a.tasks.list()[0]
        assert len(a.tasks.list()) == len(b.tasks.list()) == 1
        with pytest.raises(LookupError): b.tasks.get(task["id"])
        a.tasks.steer(task["id"], "extra details", "update-key")
        a.tasks.steer(task["id"], "extra details", "update-key")
        assert [m["content"] for m in a.conversation.list()].count("extra details") == 1
        assert not AgentContext(db, "owner").conversation.list()
