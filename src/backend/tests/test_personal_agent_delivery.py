from personal_agent.conversation.service import ConversationService
"""Independent delivery: tool -> durable main message -> owner-scoped artifact cards."""
import asyncio
import base64
import io
import json
import time
from types import SimpleNamespace

import pytest
from PIL import Image
from sqlalchemy import select

from core.db.models import Artifact, ChatMessage
from core.infra.ephemeral import LocalEphemeralState
from personal_agent.tools import tools
from personal_agent.computer import computer
from personal_agent.tasks.execution import current_execution
from personal_agent.models import PersonalWorkItem
from personal_agent.context import AgentContext
from tests.test_personal_agent import personal_agent_on, sessions  # noqa: F401


def result(response):
    return json.loads(response.content[0].text)


@pytest.fixture
def setup(sessions, monkeypatch):
    from personal_agent.delivery import delivery
    monkeypatch.setattr(delivery, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    monkeypatch.setattr(tools, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.computer.computer_wait.SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tools.email.register_email_tools", lambda *args: None)
    current_execution.set(None)
    with sessions() as db:
        svc = AgentContext(db, "owner")
        task = svc.tasks.submit("Capture and deliver a screenshot", "task")["id"]
        row = db.get(PersonalWorkItem, task)
        row.status, row.owner, row.run_id, row.lease_until = "running", "worker", "run", time.time() + 300
        db.commit()
    files = {"/workspace/work/report.txt": b"verified output"}
    class Storage:
        def __init__(self): self.files = {}
        def upload_bytes(self, content, key): self.files[key] = content; return key
        def download_bytes(self, key): return self.files[key]
        def delete(self, key): self.files.pop(key, None)
    storage = Storage()
    monkeypatch.setattr('core.files.storage.get_storage', lambda: storage)
    async def get_file(box, path, user_id=None): return files[path]
    monkeypatch.setattr(tools, "get_personal_sandbox_provider", lambda: SimpleNamespace(get_file=get_file))
    cache = LocalEphemeralState()
    monkeypatch.setattr("core.infra.ephemeral.get_ephemeral_state", lambda: cache)
    kit = []
    tools.register_personal_tools(kit, SimpleNamespace(profile_ref=task, current_user_id="owner"))
    yield SimpleNamespace(task=task, functions={tool.name: tool._func for tool in kit}, files=files, storage=storage, cache=cache)
    current_execution.set(None)


async def test_workspace_delivery_is_visible_before_task_finishes(sessions, setup, monkeypatch):
    assert "pa_deliver" in setup.functions
    from personal_agent.delivery import delivery
    monkeypatch.setattr(delivery, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    receipt = result(await setup.functions["pa_deliver"](
        request_key="report-1", path="/workspace/work/report.txt", filename="报告.txt", caption="测试文件已生成。"))
    with sessions() as db:
        svc = AgentContext(db, "owner")
        messages = svc.conversation.list()
        assert db.get(PersonalWorkItem, setup.task).status == "running"
        row = messages[-1]
        assert row["role"] == "assistant" and row["content"] == "测试文件已生成。"
        assert row["artifacts"] == [{"file_id": receipt["artifact_id"], "name": "报告.txt",
            "mime_type": "text/plain", "size": 15, "url": receipt["download_url"],
            "origin": "cloud", "chat_id": svc.identity.agent().chat_id}]
        assert receipt["delivered"] is True and receipt["message_id"] == row["id"]
        assert receipt["channel_status"] == "not_requested"


async def test_observe_then_deliver_publishes_exact_screenshot(sessions, setup, monkeypatch):
    from personal_agent.delivery import delivery
    monkeypatch.setattr(delivery, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    image = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(image, format="PNG")
    content = image.getvalue()
    async def screenshot(*args):
        return {"image_base64": base64.b64encode(content).decode(), "width": 8, "height": 8,
                "observation_id": "observation", "signature": "signature", "platform": "Linux"}
    monkeypatch.setattr(computer, "cloud_driver", screenshot)
    observed = result(await setup.functions["pa_computer_observe"]())
    assert observed["resource_ref"]
    with sessions() as db:
        assert list(db.scalars(select(Artifact))) == []  # Observation is not delivery.
    receipt = result(await setup.functions["pa_deliver"](
        request_key="screen-1", resource_ref=observed["resource_ref"], filename="云电脑截图.png"))
    with sessions() as db:
        file = db.get(Artifact, receipt["artifact_id"])
        assert setup.storage.files[file.storage_key] == content
        card = AgentContext(db, "owner").conversation.list()[-1]["artifacts"][0]
        assert card["mime_type"] == "image/png"
        assert card["name"] == "云电脑截图.png"


async def test_delivery_retry_recovers_immutable_receipt_without_duplicate(sessions, setup):
    args = dict(request_key="report", path="/workspace/work/report.txt", filename="报告.txt")
    first = result(await setup.functions["pa_deliver"](**args))
    setup.files.clear()
    with sessions() as db:
        row = db.get(PersonalWorkItem, setup.task)
        row.owner, row.run_id = "replacement", "run-2"
        db.commit()
    assert result(await setup.functions["pa_deliver"](**args)) == first
    with sessions() as db:
        assert len(list(db.scalars(select(Artifact)))) == 1
        assert len([m for m in AgentContext(db, "owner").conversation.list() if m["role"] == "assistant"]) == 1


async def test_request_key_conflict_never_replaces_delivered_file(sessions, setup):
    first = result(await setup.functions["pa_deliver"](request_key="report", path="/workspace/work/report.txt"))
    with pytest.raises(ValueError, match="different input"):
        await setup.functions["pa_deliver"](request_key="report", path="/workspace/work/another.txt")
    with sessions() as db:
        item = db.get(Artifact, first["artifact_id"])
        assert setup.storage.files[item.storage_key] == b"verified output"


@pytest.mark.parametrize("patch", [
    {"path": "/etc/passwd"}, {"path": "/workspace/../etc/passwd"},
    {"path": "https://example.test/file"}, {"path": "/workspace/file\x00"},
    {"path": "/workspace/work/report.txt", "resource_ref": "screen_forged"},
    {"resource_ref": "screen_forged"}, {"path": "/workspace/work/report.txt", "filename": "../secret"},
])
async def test_invalid_sources_never_publish(sessions, setup, patch):
    with pytest.raises((ValueError, PermissionError)):
        await setup.functions["pa_deliver"](request_key="bad", **patch)
    with sessions() as db:
        assert list(db.scalars(select(Artifact))) == []


@pytest.mark.parametrize("change", ["cancel", "expired_lease", "pending_approval", "capability", "generation", "owner"])
async def test_access_is_rechecked_after_source_read(sessions, setup, monkeypatch, change):
    from personal_agent.delivery import delivery
    allowed = True
    monkeypatch.setattr(delivery.access, "allowed", lambda *args: allowed)
    async def changed(*args, **kwargs):
        nonlocal allowed
        with sessions() as db:
            row = db.get(PersonalWorkItem, setup.task)
            if change == "cancel": row.status = "cancelled"
            elif change == "expired_lease": row.lease_until = time.time() - 1
            elif change == "pending_approval": row.checkpoint = {**row.checkpoint, "pending_approval": {"id": "approval"}}
            elif change == "capability": allowed = False
            elif change == "owner": row.owner = "replacement"
            else: AgentContext(db, "owner").identity.agent().generation += 1
            db.commit()
        return b"verified output"
    monkeypatch.setattr(tools, "get_personal_sandbox_provider", lambda: SimpleNamespace(get_file=changed))
    with pytest.raises(PermissionError):
        await setup.functions["pa_deliver"](request_key="cancel-race", path="/workspace/work/report.txt")
    with sessions() as db:
        assert list(db.scalars(select(Artifact))) == []


async def test_storage_or_message_failure_never_claims_delivery(sessions, setup, monkeypatch):
    deliver = setup.functions["pa_deliver"]
    upload = setup.storage.upload_bytes
    def fail(*args, **kwargs): raise OSError("unavailable")
    monkeypatch.setattr(setup.storage, "upload_bytes", fail)
    with pytest.raises(OSError):
        await deliver(request_key="retry", path="/workspace/work/report.txt")
    monkeypatch.setattr(setup.storage, "upload_bytes", upload)
    original = ConversationService.append
    monkeypatch.setattr(ConversationService, "append", fail)
    with pytest.raises(OSError):
        await deliver(request_key="retry", path="/workspace/work/report.txt")
    with sessions() as db:
        assert list(db.scalars(select(Artifact))) == []
        assert setup.storage.files == {}
        assert [m["role"] for m in AgentContext(db, "owner").conversation.list()] == ["user"]
    monkeypatch.setattr(ConversationService, "append", original)
    assert result(await deliver(request_key="retry", path="/workspace/work/report.txt"))["delivered"]


async def capture(setup, monkeypatch):
    import os
    from pathlib import Path
    image = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(image, format="PNG")
    # Optional runtime probe: exercise delivery with the real driver's captured bytes.
    content = Path(os.environ["PA_DELIVERY_SCREENSHOT_PNG"]).read_bytes() if os.environ.get("PA_DELIVERY_SCREENSHOT_PNG") else image.getvalue()
    with Image.open(io.BytesIO(content)) as frame:
        width, height = frame.size
    async def screenshot(*args):
        return {"image_base64": base64.b64encode(content).decode(), "width": width, "height": height,
                "observation_id": "observation", "signature": "signature", "platform": "Linux"}
    monkeypatch.setattr(computer, "cloud_driver", screenshot)
    return result(await setup.functions["pa_computer_observe"]())


@pytest.mark.parametrize("change", ["expired", "other_user", "other_task", "run", "generation", "owner"])
async def test_screenshot_reference_cannot_escape_its_scope(sessions, setup, monkeypatch, change):
    from personal_agent.delivery import delivery
    observed = await capture(setup, monkeypatch)
    deliver = setup.functions["pa_deliver"]
    if change == "expired":
        await setup.cache.drop(*(await setup.cache.keys("pa:delivery-resource:")))
    elif change in {"other_user", "other_task"}:
        with sessions() as db:
            uid = "other" if change == "other_user" else "owner"
            other = AgentContext(db, uid).tasks.submit("Other task", "other")["id"]
            row = db.get(PersonalWorkItem, other)
            row.status, row.owner, row.run_id, row.lease_until = "running", "worker", "run", time.time() + 300
            db.commit()
        deliver = delivery.build(other, uid)
    else:
        with sessions() as db:
            row = db.get(PersonalWorkItem, setup.task)
            if change == "run": row.run_id = "replacement"
            elif change == "owner": row.owner = "replacement"
            else: AgentContext(db, "owner").identity.agent().generation += 1
            db.commit()
    with pytest.raises((ValueError, PermissionError)):
        await deliver(request_key="screen", resource_ref=observed["resource_ref"])
    with sessions() as db:
        assert list(db.scalars(select(Artifact))) == []


async def test_observation_cache_is_bounded_and_receipt_survives_expiry(sessions, setup, monkeypatch):
    refs = [(await capture(setup, monkeypatch))["resource_ref"] for _ in range(6)]
    assert len(await setup.cache.keys("pa:delivery-resource:")) == 4
    with pytest.raises(ValueError, match="Observe again"):
        await setup.functions["pa_deliver"](request_key="old", resource_ref=refs[0])
    delivered = result(await setup.functions["pa_deliver"](request_key="latest", resource_ref=refs[-1]))
    await setup.cache.drop(*(await setup.cache.keys("pa:delivery-resource:")))
    assert result(await setup.functions["pa_deliver"](request_key="latest", resource_ref=refs[-1])) == delivered


async def test_screenshot_file_and_channel_end_to_end(sessions, setup, monkeypatch):
    from integrations.channels.protocol import InboundMsg, SendResult
    from personal_agent.channels import channel_bridge
    monkeypatch.setattr(channel_bridge, "SessionLocal", sessions)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", sessions)
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.identity.agent().config = {**svc.identity.agent().config, "channel_ids": ["bot"]}
        user = db.get(ChatMessage, svc.conversation.list()[0]["id"])
        origin = {"channel_id": "bot", "channel_type": "weixin", "msg": channel_bridge.pack(
            InboundMsg(channel_id="bot", channel_type="weixin", text="", external_conversation_id="private",
                       chat_type="p2p"))}
        user.extra_data = {**user.extra_data, "channel_origin": origin}
        db.commit()
    observed = await capture(setup, monkeypatch)
    image = result(await setup.functions["pa_deliver"](request_key="screen", resource_ref=observed["resource_ref"],
                                                     caption="截图已交付。", filename="云电脑截图.png"))
    report = result(await setup.functions["pa_deliver"](request_key="report", path="/workspace/work/report.txt",
                                                      filename="报告.txt"))
    assert image["channel_status"] == "pending" and report["channel_status"] == "pending"
    sent, attempts = [], []
    class Adapter:
        caps = SimpleNamespace(supports_markdown=False, max_message_len=0)
        async def send_text(self, conn, msg, text): sent.append(("text", text)); return SendResult.ok()
        async def push_file(self, conn, msg, content, name, mime):
            attempts.append(name)
            if name == "云电脑截图.png" and attempts.count(name) == 1: return SendResult.fail("transient")
            sent.append(("file", name, mime)); return SendResult.ok()
    monkeypatch.setattr(channel_bridge, "connection", lambda ident: SimpleNamespace(
        channel_id="bot", channel_type="weixin", owner_user_id="owner"))
    monkeypatch.setattr('integrations.channels.registry.get_adapter', lambda kind: Adapter())
    await channel_bridge.deliver_pending()
    await channel_bridge.deliver_pending()
    with sessions() as db:
        svc = AgentContext(db, "owner")
        svc.conversation.append(svc.identity.agent(), "assistant", "已完成：" + image["download_url"], setup.task)
        db.commit()
        messages = svc.conversation.list()
        # A final report must not send the same delivered image again.
        assert messages[1]["artifacts"][0]["file_id"] == image["artifact_id"]
        assert len(messages[2]["artifacts"]) == 1
        assert AgentContext(db, "other").conversation.list() == []
    await channel_bridge.deliver_pending()
    assert [item[1] for item in sent if item[0] == "file"] == ["报告.txt", "云电脑截图.png"]
    assert attempts == ["云电脑截图.png", "报告.txt", "云电脑截图.png"]
    assert len(list(setup.storage.files)) == 2
    import os
    if target := os.environ.get("PA_DELIVERY_BROWSER_DATA"):
        from pathlib import Path
        # Export only isolated fixture data for the production browser components.
        files = {}
        with sessions() as db:
            for item in db.scalars(select(Artifact)):
                files[item.artifact_id] = {"mime": item.mime_type,
                    "data": base64.b64encode(setup.storage.files[item.storage_key]).decode(),
                    "name": item.extra_data["original_filename"]}
        Path(target).write_text(json.dumps({"messages": [row for row in messages if row.get("artifacts")], "files": files}, ensure_ascii=False))


async def test_simultaneous_sqlite_deliveries_use_one_artifact_and_message(sessions, setup, monkeypatch, tmp_path):
    import sqlite3
    import threading
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from personal_agent.delivery import delivery
    path = tmp_path / "deliveries.sqlite"
    with sqlite3.connect(path) as target:
        source = sessions.kw["bind"].raw_connection()
        try: source.driver_connection.backup(target)
        finally: source.close()
    engine = create_engine("sqlite:///" + str(path), connect_args={"timeout": 15})
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(delivery, "SessionLocal", factory)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", factory)
    monkeypatch.setattr(tools, "SessionLocal", factory)
    monkeypatch.setattr("personal_agent.tasks.guards.SessionLocal", factory)
    barrier, guard = threading.Barrier(2), threading.Lock()
    reads = 0
    @event.listens_for(engine, "after_cursor_execute")
    def race(conn, cursor, statement, parameters, context, executemany):
        nonlocal reads
        if (threading.current_thread() is not threading.main_thread()
                and statement.startswith("SELECT") and "WHERE personal_work_items.id =" in statement):
            with guard:
                reads += 1
                synchronize = reads <= 2
            if synchronize: barrier.wait(timeout=5)
    try:
        args = dict(request_key="same", path="/workspace/work/report.txt")
        one, two = await asyncio.gather(setup.functions["pa_deliver"](**args), setup.functions["pa_deliver"](**args))
        assert result(one) == result(two)
        with factory() as db:
            assert len(list(db.scalars(select(Artifact)))) == 1
            assert len([r for r in AgentContext(db, "owner").conversation.list() if r["role"] == "assistant"]) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("change", ["revoke", "epoch", "consent"])
async def test_paired_screenshot_revalidates_device_before_delivery(sessions, setup, monkeypatch, change):
    from personal_agent.models import PersonalDevice
    with sessions() as db:
        db.add(PersonalDevice(id="paired", user_id="owner", name="Device", platform="linux",
                              secret_hash="unused", epoch=1, control_enabled=1, capabilities=["observe"]))
        db.commit()
    image = io.BytesIO()
    Image.new("RGB", (8, 8)).save(image, format="PNG")
    async def operate(*args, **kwargs):
        return {"image_base64": base64.b64encode(image.getvalue()).decode(), "width": 8, "height": 8,
                "observation_id": "paired-observation"}
    monkeypatch.setattr(computer, "operate", operate)
    observed = result(await setup.functions["pa_computer_observe"](target="device:paired"))
    with sessions() as db:
        device = db.get(PersonalDevice, "paired")
        if change == "revoke": device.revoked = 1
        elif change == "epoch": device.epoch += 1
        else: device.control_enabled = 0
        db.commit()
    with pytest.raises(PermissionError):
        await setup.functions["pa_deliver"](request_key="paired", resource_ref=observed["resource_ref"])
    assert setup.storage.files == {}


async def test_permission_loss_after_upload_compensates_the_object(sessions, setup, monkeypatch):
    from personal_agent.delivery import delivery
    allowed = True
    monkeypatch.setattr(delivery.access, "allowed", lambda *args: allowed)
    original = setup.storage.upload_bytes
    def upload(content, key):
        nonlocal allowed
        value = original(content, key)
        allowed = False
        return value
    monkeypatch.setattr(setup.storage, "upload_bytes", upload)
    with pytest.raises(PermissionError):
        await setup.functions["pa_deliver"](request_key="upload-race", path="/workspace/work/report.txt")
    with sessions() as db:
        assert list(db.scalars(select(Artifact))) == []
    assert setup.storage.files == {}


async def test_oversized_file_and_invalid_screenshot_are_not_delivered(sessions, setup, monkeypatch):
    from personal_agent.delivery.delivery import MAX_BYTES
    setup.files["/workspace/work/big"] = b"x" * (MAX_BYTES + 1)
    with pytest.raises(ValueError, match="20 MiB"):
        await setup.functions["pa_deliver"](request_key="oversized", path="/workspace/work/big")
    async def invalid(*args):
        return {"image_base64": "not-a-valid-image", "observation_id": "bad"}
    monkeypatch.setattr(computer, "cloud_driver", invalid)
    with pytest.raises((ValueError, OSError)):
        await setup.functions["pa_computer_observe"]()
    assert setup.storage.files == {}


async def test_paired_observation_routes_real_device_id(sessions, setup, monkeypatch):
    from personal_agent.models import PersonalDevice
    with sessions() as db:
        db.add(PersonalDevice(id="paired", user_id="owner", name="Laptop",
            platform="Linux", secret_hash="unused", capabilities=["observe"], control_enabled=True, epoch=1))
        db.commit()
    calls = []
    async def execute(ident, uid, device_id, action):
        calls.append((ident, uid, device_id, action))
        image = io.BytesIO()
        Image.new("RGB", (8, 8)).save(image, format="PNG")
        return {"image_base64": base64.b64encode(image.getvalue()).decode(), "observation_id": "paired-observe"}
    monkeypatch.setattr("personal_agent.computer.devices.execute_command", execute)
    observed = result(await setup.functions["pa_computer_observe"](target="device:paired"))
    assert observed["resource_ref"]
    assert calls == [(setup.task, "owner", "paired", {"type": "observe"})]
