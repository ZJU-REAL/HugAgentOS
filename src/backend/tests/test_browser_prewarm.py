"""Conversation resource lifecycle: warming never duplicates the first open."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest
import httpx
from core.db.models import ChatSession
from core.plugins.resources import installation, service
from core.sandbox import interactive, factory

HTTPClient = httpx.AsyncClient

@pytest.fixture
def test_database_url(tmp_path):
    return "sqlite:///" + str(tmp_path / "prewarm.db")

@pytest.fixture
def runtime(db_session, monkeypatch):
    monkeypatch.setenv("PLUGIN_RESOURCE_SECRET_KEY", "isolated-prewarm-fixture")
    db_session.add(ChatSession(chat_id="warm-chat", user_id="warm-user", title="warm"))
    db_session.commit()
    module = {"id": "browser", "resource": {"entry": "runtime", "callable": "worker:main", "prewarm": True}}
    selected = installation.Installation("warm-install", "rev-1", "browser", {"contributes": {"modules": [module]}}, Path("/fixture"))
    monkeypatch.setattr(installation, "resolve", lambda *a, **k: selected)
    launches = []
    async def launch(provider, selected, module, user_id, chat_id, config):
        launches.append(config)
        return {"url": "http://fixture", "headers": {}, "sandbox_id": "sandbox-1", "provider": "fixture", "process_id": "process-1"}
    async def identity(chat_id):
        return "sandbox-1"
    provider = SimpleNamespace(name="fixture", current_sandbox_id=identity)
    monkeypatch.setattr(interactive, "launch", launch)
    monkeypatch.setattr(factory, "get_sandbox_provider", lambda: provider)
    async def request(*a, **k):
        return {"closed": False}
    monkeypatch.setattr(service, "request", request)
    client = HTTPClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"closed": False}))))
    return db_session, launches, selected, provider

async def test_first_open_adopts_warm_worker_once(runtime):
    db, launches, _, _ = runtime
    warm = await service.create(db, "browser", "browser", "warm-user", "warm-chat", _prewarm=True)
    assert warm["status"] == "warm"
    opened = await service.create(db, "browser", "browser", "warm-user", "warm-chat")
    assert opened["resource_id"] == warm["resource_id"]
    assert opened["status"] == "active"
    again = await service.create(db, "browser", "browser", "warm-user", "warm-chat")
    assert again["resource_id"] != warm["resource_id"]
    assert len(launches) == 2

async def test_open_joins_inflight_warm_start(runtime, monkeypatch):
    db, launches, _, _ = runtime
    entered, release = asyncio.Event(), asyncio.Event()
    original = interactive.launch
    async def slow_launch(*args):
        entered.set()
        await release.wait()
        return await original(*args)
    monkeypatch.setattr(interactive, "launch", slow_launch)
    warming = asyncio.create_task(service.create(db, "browser", "browser", "warm-user", "warm-chat", _prewarm=True))
    await entered.wait()
    # A separate request session observes the committed start reservation.
    from sqlalchemy.orm import Session
    with Session(db.bind) as other:
        opened = asyncio.create_task(service.create(other, "browser", "browser", "warm-user", "warm-chat"))
        await asyncio.sleep(0.1)
        assert not opened.done()
        release.set()
        warm, active = await asyncio.gather(warming, opened)
    assert warm["resource_id"] == active["resource_id"]
    assert len(launches) == 1

async def test_router_starts_warm_worker_in_background_and_close_reclaims_it(runtime, monkeypatch):
    from core.plugins.resources import prewarm
    from core.db.models import InstalledPlugin
    from core.capabilities import device_catalog
    from core.sandbox.session_router import SessionSandboxRouter
    from sqlalchemy.orm import sessionmaker
    db, launches, selected, provider = runtime
    db.add(InstalledPlugin(install_id=selected.install_id, slug=selected.slug, name="Browser", owner_user_id="warm-user"))
    db.commit()
    monkeypatch.setattr(device_catalog, "active", lambda: False)
    monkeypatch.setattr(prewarm.engine, "SessionLocal", sessionmaker(bind=db.bind))
    ensured, entered, release = [], asyncio.Event(), asyncio.Event()
    original = interactive.launch
    async def launch(*args):
        entered.set()
        await release.wait()
        return await original(*args)
    async def ensure(*args):
        ensured.append(args)
    async def close(*args):
        pass
    provider.ensure_user_workspace = ensure
    provider.close_session = close
    monkeypatch.setattr(interactive, "launch", launch)
    router = SessionSandboxRouter(provider)
    await asyncio.wait_for(router.ensure_user_workspace("warm-chat", "warm-user"), 1)
    await asyncio.wait_for(entered.wait(), 1)
    assert ensured and not launches
    release.set()
    for _ in range(100):
        await asyncio.sleep(0.01)
        with prewarm.engine.SessionLocal() as check:
            from core.plugins.resources import store
            rows = check.query(store.PluginResource).filter_by(status="warm").all()
            if rows:
                resource_id = rows[0].resource_id
                break
    else:
        pytest.fail("background worker never became ready")
    assert len(launches) == 1
    await router.close_session("warm-chat")
    with pytest.raises(Exception) as expired:
        service.authorized(db, resource_id, "warm-user")
    assert expired.value.status_code == 410

async def test_warm_is_private_and_counts_against_quota(runtime, monkeypatch):
    from fastapi import HTTPException
    db, launches, _, _ = runtime
    monkeypatch.setenv("PLUGIN_RESOURCE_MAX_PER_USER", "1")
    warm = await service.create(db, "browser", "browser", "warm-user", "warm-chat", _prewarm=True)
    with pytest.raises(HTTPException) as unavailable:
        service.authorized(db, warm["resource_id"], "warm-user")
    assert unavailable.value.status_code == 410
    db.add(ChatSession(chat_id="second-chat", user_id="warm-user", title="Second"))
    db.commit()
    with pytest.raises(HTTPException) as limited:
        await service.create(db, "browser", "browser", "warm-user", "second-chat")
    assert limited.value.status_code == 429
    assert len(launches) == 1
    active = await service.create(db, "browser", "browser", "warm-user", "warm-chat")
    assert active["resource_id"] == warm["resource_id"]

async def test_checkpoint_opens_its_own_worker_and_never_consumes_blank_warm(runtime):
    from core.plugins.resources import store
    from core.plugins.resources.crypto import encrypt_secret
    db, launches, selected, _ = runtime
    warm = await service.create(db, "browser", "browser", "warm-user", "warm-chat", _prewarm=True)
    db.add(store.PluginResourceCheckpoint(checkpoint_id="saved", user_id="warm-user",
        install_id=selected.install_id, name="Saved", state_enc=encrypt_secret('{"cookies":[]}')))
    db.commit()
    restored = await service.create(db, "browser", "browser", "warm-user", "warm-chat", checkpoint_id="saved")
    assert restored["resource_id"] != warm["resource_id"]
    assert launches[-1]["checkpoint"] == {"cookies": []}

@pytest.mark.parametrize("change", ["expired", "sandbox", "revision"])
async def test_invalid_warm_is_never_reused(runtime, monkeypatch, change):
    from core.plugins.resources import store
    db, launches, selected, provider = runtime
    warm = await service.create(db, "browser", "browser", "warm-user", "warm-chat", _prewarm=True)
    if change == "expired":
        db.get(store.PluginResource, warm["resource_id"]).expires_at = 0
        db.commit()
    elif change == "sandbox":
        async def identity(chat):
            return "sandbox-2"
        provider.current_sandbox_id = identity
    else:
        newer = installation.Installation(selected.install_id, "rev-2", selected.slug, selected.ui, selected.package)
        monkeypatch.setattr(installation, "resolve", lambda *a, **kw: newer)
    active = await service.create(db, "browser", "browser", "warm-user", "warm-chat")
    assert active["resource_id"] != warm["resource_id"]
    assert len(launches) == 2

async def test_revoked_chat_during_prewarm_cannot_publish_ready_worker(runtime, monkeypatch):
    from fastapi import HTTPException
    db, _, _, provider = runtime
    cancelled = []
    original = interactive.launch
    async def launch(*args):
        descriptor = await original(*args)
        db.get(ChatSession, "warm-chat").user_id = "another-owner"
        db.commit()
        return descriptor
    async def write(*args, **kwargs):
        cancelled.append(kwargs["chars"])
    provider.write_stdin = write
    monkeypatch.setattr(interactive, "launch", launch)
    with pytest.raises(HTTPException) as denied:
        await service.create(db, "browser", "browser", "warm-user", "warm-chat", _prewarm=True)
    assert denied.value.status_code == 403
    assert cancelled == ["\x03"]

async def test_initial_managed_start_cancellation_retains_handle_until_cleanup(tmp_path, monkeypatch):
    from core.plugins.resources import confinement
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "worker.py").write_text("def main(config): pass")
    selected = installation.Installation("install", "rev", "browser", {}, tmp_path)
    module = {"resource": {"entry": "runtime", "callable": "worker:main"}}
    entered, release = asyncio.Event(), asyncio.Event()
    cleaned = []
    async def start(req, yield_time_ms):
        entered.set()
        await release.wait()
        return {"session_id": "owned-start", "output": ""}
    async def write(handle, **kwargs):
        cleaned.append((handle, kwargs["chars"]))
    provider = SimpleNamespace(start_process=start, write_stdin=write)
    monkeypatch.setattr(confinement, "launch_policy", lambda *a: {})
    task = asyncio.create_task(interactive.launch(provider, selected, module, "owner", "chat", {"token": "fixture-only"}))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned == [("owned-start", "\x03")]

async def test_close_fence_blocks_readmission_until_provider_close_finishes(runtime, monkeypatch):
    from core.plugins.resources import prewarm
    from core.sandbox.session_router import SessionSandboxRouter
    from sqlalchemy.orm import sessionmaker
    db, launches, selected, provider = runtime
    from core.db.models import InstalledPlugin
    from core.capabilities import device_catalog
    db.add(InstalledPlugin(install_id=selected.install_id, slug=selected.slug, name="Browser", owner_user_id="warm-user"))
    db.commit()
    monkeypatch.setattr(device_catalog, "active", lambda: False)
    monkeypatch.setattr(prewarm.engine, "SessionLocal", sessionmaker(bind=db.bind))
    entered, release = asyncio.Event(), asyncio.Event()
    async def close(chat):
        entered.set()
        await release.wait()
    provider.close_session = close
    task = asyncio.create_task(SessionSandboxRouter(provider).close_session("warm-chat"))
    await entered.wait()
    prewarm.schedule(provider, "warm-chat", "warm-user", "sandbox-1")
    await asyncio.sleep(.05)
    assert not launches
    release.set()
    await task

async def test_replacement_launch_can_retire_old_processes_without_cancelling_itself():
    from core.sandbox.process_sessions import ProcessSessions
    sessions = ProcessSessions()
    stopped = []
    class Handle:
        async def poll(self):
            return "", "", None
        async def close(self):
            stopped.append(self)
        async def interrupt(self):
            pass
    old_handle, new_handle = Handle(), Handle()
    async def old():
        return old_handle
    await sessions.start(old, ("chat", "owner"), yield_time_ms=0)
    async def replace():
        await sessions.close_owner("chat")
        return new_handle
    current = await asyncio.wait_for(sessions.start(replace, ("chat", "owner"), yield_time_ms=0), 1)
    assert current["status"] == "running"
    assert old_handle in stopped and new_handle not in stopped
    await sessions.close_all()
    assert new_handle in stopped

@pytest.mark.parametrize("sandbox,stale", [(None, False), ("other", False), ("bound", True)])
def test_background_binding_rejects_changed_sandbox_and_resets_after_failure(sandbox, stale):
    from core.sandbox.bound_session import bind, require_available
    from core.sandbox.errors import SandboxError
    with pytest.raises(SandboxError):
        with bind("bound"):
            require_available(sandbox, stale=stale)
    # A normal foreground operation can allocate a replacement after this scope.
    require_available(None, stale=True)

def test_background_binding_accepts_its_live_sandbox():
    from core.sandbox.bound_session import bind, require_available
    with bind("bound"):
        require_available("bound")

async def test_failed_warm_shutdown_reports_sandbox_dirty(runtime, monkeypatch):
    from core.plugins.resources import prewarm
    from sqlalchemy.orm import sessionmaker
    db, _, _, _ = runtime
    monkeypatch.setattr(prewarm.engine, "SessionLocal", sessionmaker(bind=db.bind))
    await service.create(db, "browser", "browser", "warm-user", "warm-chat", _prewarm=True)
    db.commit()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: HTTPClient(
        **kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(503))))
    assert await prewarm.stop("warm-chat") is False
