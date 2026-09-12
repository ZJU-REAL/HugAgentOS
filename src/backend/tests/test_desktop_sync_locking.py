"""SQLite publication must not wait on a writer that waits for account identity."""

import base64
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from core.capabilities import registry
from core.capabilities.ref import cloud_ref
from core.db.engine import apply_sqlite_concurrency_pragmas
from core.db.models import ContentBlock, DeviceCapabilityInstallation, UserShadow, ChatSession
from core.db.models.observability import DesktopOutbox
from core.services import desktop_cloud_bridge as bridge
from core.services.desktop_observability_sync import install_capture


@pytest.mark.parametrize("observed", [False, True])
def test_sqlite_writer_commits_while_capability_publication_holds_account_lock(
    tmp_path, monkeypatch, observed
):
    engine = create_engine(
        "sqlite:///" + str(tmp_path / "index.db"), connect_args={"check_same_thread": False}
    )
    apply_sqlite_concurrency_pragmas(engine, 1)
    for model in (
        ContentBlock,
        DeviceCapabilityInstallation,
        UserShadow,
        ChatSession,
        DesktopOutbox,
    ):
        model.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(registry, "SessionLocal", factory)
    claims = (
        base64.urlsafe_b64encode(json.dumps({"u": "alice", "c": "center", "d": "device"}).encode())
        .decode()
        .rstrip("=")
    )
    state = {
        "cloud_base": "https://cloud.example",
        "token": f"dcap2.{claims}.sig",
        "expires_at": 9999999999,
    }
    monkeypatch.setattr(bridge, "_state", state)
    monkeypatch.setattr(bridge, "_state_loaded", True)
    identity = bridge.get_identity_state()
    with factory() as db:
        db.add(
            UserShadow(
                user_id="alice", user_center_id=identity["shell_user_center_id"], username="Alice"
            )
        )
        db.commit()
    inst = registry.upsert(
        profile_id="account",
        ref=cloud_ref("https://cloud.example", "skill", "test", scope="private"),
    )
    wrote, continue_flush = threading.Event(), threading.Event()

    def pause_after_write(db, context):
        if db.info.get("test_writer"):
            wrote.set()
            assert continue_flush.wait(3)

    event.listen(factory, "after_flush", pause_after_write)
    remove = install_capture(factory, bridge.get_identity_state)

    def write():
        with factory() as db:
            db.info["test_writer"] = True
            db.add(
                ChatSession(chat_id="chat", user_id="alice", title="Task")
                if observed
                else ContentBlock(id="prefs", payload={})
            )
            db.commit()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            writer = pool.submit(write)
            assert wrote.wait(3)
            with bridge.account_scope(state):
                continue_flush.set()
                assert registry.set_state(inst.install_id, "preparing").state == "preparing"
            writer.result(timeout=3)
        if observed:
            with factory() as db:
                assert (
                    db.query(DesktopOutbox).filter_by(kind="session", object_id="chat").count() == 1
                )
    finally:
        continue_flush.set()
        remove()
        event.remove(factory, "after_flush", pause_after_write)
        engine.dispose()


@pytest.mark.asyncio
async def test_desktop_authentication_does_not_block_the_event_loop(monkeypatch):
    import asyncio
    from core.auth import backend, desktop_bridge
    from starlette.requests import Request

    released = threading.Event()
    progressed = []

    loop = asyncio.get_running_loop()

    def authenticate(request, db):
        loop.call_soon_threadsafe(released.set)
        progressed.append(released.wait(2))
        return backend.UserContext(user_id="alice", user_center_id="center", username="Alice")

    monkeypatch.setattr(desktop_bridge, "resolve_bridge_user", authenticate)
    try:
        result = await backend.get_current_user(
            Request({"type": "http", "headers": []}), None, None
        )
        assert result.user_id == "alice"
        assert progressed == [True], "authentication blocked the event loop"
    finally:
        released.set()
