"""Issue #265: controller inventory and session reservations must converge."""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import core.sandbox.providers.opensandbox.lifecycle as runtime
import core.sandbox.sessions.registry as session_registry
from core.sandbox.providers.opensandbox.models import SandboxSessionRecord as _Session
from core.sandbox.providers.opensandbox.user_pool import _JupyterUserPool
from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider


def _worker(monkeypatch, response):
    monkeypatch.setattr(runtime, "settings", SimpleNamespace(sandbox=SimpleNamespace(idle_ttl_s=3600)))
    worker = OpenSandboxProvider.__new__(OpenSandboxProvider)
    worker._sessions = {}
    worker._parking_in_flight = set()
    worker._session_locks = {}
    worker._registry_lock = threading.Lock()
    worker._domain = "http://controller"
    worker._get_http_client = lambda: SimpleNamespace(post=AsyncMock(return_value=response))
    worker._wait_snapshot_ready = AsyncMock()
    worker._upsert_snapshot = AsyncMock(return_value=None)
    worker._destroy_sandbox = AsyncMock()
    worker._renew = AsyncMock()
    worker._jupyter_user_pool = _JupyterUserPool(
        factory=AsyncMock(return_value=SimpleNamespace(id="gone")),
        destroy_fn=worker._destroy_sandbox,
        max_idle_per_user=1,
        max_in_flight_per_user=1,
    )
    return worker


async def _bind(worker, chat="chat"):
    sbx = await worker._jupyter_user_pool.acquire("user")
    sess = _Session(sandbox=sbx,  user_id="user", pool_source="user",
                    last_active_ts=time.monotonic()-7200)
    worker._sessions[chat] = sess
    await session_registry.remember(chat, str(sbx.id))
    from tests.sandbox.test_session_affinity import _age
    await _age(session_registry.get_ephemeral_state(), chat, 7200)
    return sess


def test_missing_container_releases_reservation_and_next_acquire_succeeds(monkeypatch, provider_database):
    worker = _worker(monkeypatch, httpx.Response(404, json={"code":"DOCKER::SANDBOX_NOT_FOUND"}))

    async def scenario():
        sess = await _bind(worker)
        # The actual snapshot HTTP boundary reports the production failure.
        await worker._park_session_via_snapshot("chat", sess)
        await worker._park_session_via_snapshot("chat", sess)
        assert worker._sessions == {}
        assert worker._jupyter_user_pool.stats()["user"] == {"idle":0,"in_flight":0}
        await worker._jupyter_user_pool.acquire("user")
        assert worker._jupyter_user_pool.stats()["user"]["in_flight"] == 1
        worker._destroy_sandbox.assert_awaited_once()
        worker._upsert_snapshot.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("response", [
    httpx.Response(404, json={"code":"SNAPSHOT::NOT_FOUND"}),
    httpx.Response(404, text="proxy route not found"),
    httpx.Response(401, json={"code":"UNAUTHORIZED"}),
    httpx.Response(500, json={"code":"DOCKER::INTERNAL_ERROR"}),
    httpx.ReadTimeout("controller busy"),
])
def test_ambiguous_snapshot_failures_preserve_session_and_reservation(monkeypatch, response):
    worker = _worker(monkeypatch, response)
    if isinstance(response, Exception):
        worker._get_http_client = lambda: SimpleNamespace(post=AsyncMock(side_effect=response))

    async def scenario():
        sess = await _bind(worker)
        await worker._park_session_via_snapshot("chat", sess)
        assert worker._sessions["chat"] is sess
        assert worker._jupyter_user_pool.stats()["user"]["in_flight"] == 1
        worker._destroy_sandbox.assert_not_awaited()

    asyncio.run(scenario())


def test_missing_cleanup_preserves_newer_binding(monkeypatch, provider_database):
    worker = _worker(monkeypatch, httpx.Response(404,json={"code":"DOCKER::SANDBOX_NOT_FOUND"}))

    async def scenario():
        sess = await _bind(worker)
        await session_registry.remember("chat", "replacement")
        # Age the new binding so the old local handle is eligible for retirement.
        from core.sandbox.sessions.registry import _KEY
        import json
        store = session_registry.get_ephemeral_state()
        raw = json.loads(await store.get(_KEY.format(session_id="chat")))
        raw["active_at"] -= 7200
        await store.put(_KEY.format(session_id="chat"), json.dumps(raw), ttl=7200)
        await worker._park_session_via_snapshot("chat",sess)
        assert await session_registry.lookup("chat") == "replacement"
        assert worker._jupyter_user_pool.stats()["user"]["in_flight"] == 0

    asyncio.run(scenario())


def test_controller_inventory_matches_across_workers_despite_stale_local_handles(monkeypatch):
    a = _worker(monkeypatch, None)
    b = _worker(monkeypatch, None)
    a._sessions = {"old": SimpleNamespace(sandbox=SimpleNamespace(id="gone"))}
    b._sessions = {}
    infos = [
        SimpleNamespace(id="live",status=SimpleNamespace(state="Running"),
                        metadata={"hugagent-owner":"backend","hugagent-pool-kind":"light"}),
        SimpleNamespace(id="stopped",status=SimpleNamespace(state="Terminated"),metadata={}),
    ]
    async def inventory():
        for info in infos:
            yield None,info
    for worker in (a,b):
        worker._iter_server_sandbox_infos = inventory

    async def scenario():
        first = await a.admin_list_sandboxes()
        second = await b.admin_list_sandboxes()
        assert [x.sandbox_id for x in first] == ["live"]
        assert first == second
    asyncio.run(scenario())


def test_controller_failure_is_not_reported_as_empty_inventory(monkeypatch):
    worker = _worker(monkeypatch,None)
    async def inventory():
        raise TimeoutError("controller unavailable")
        yield
    worker._iter_server_sandbox_infos = inventory
    with pytest.raises(TimeoutError):
        asyncio.run(worker.admin_list_sandboxes())


def test_repeated_renew_timeouts_do_not_mark_a_live_session_stale(monkeypatch):
    worker = _worker(monkeypatch, None)
    worker._renew = AsyncMock(side_effect=httpx.ReadTimeout("controller busy"))

    async def scenario():
        sess = await _bind(worker)
        for _ in range(4):
            await worker._renew_in_background("chat", sess)
        assert not sess.stale_marked
        assert sess.consecutive_renew_failures == 4
        assert worker._sessions["chat"] is sess

    asyncio.run(scenario())


def test_repeated_release_during_workspace_cleanup_only_returns_one_reservation(monkeypatch):
    worker = _worker(monkeypatch, None)

    async def scenario():
        sess = await _bind(worker)
        entered, resume = asyncio.Event(), asyncio.Event()
        async def wipe(_):
            entered.set()
            await resume.wait()
            return True
        worker._wipe_workspace = wipe
        reuse = asyncio.create_task(worker._release_session("chat",sess,reuse_sandbox=True))
        await entered.wait()
        await worker._release_session("chat",sess,reuse_sandbox=False)
        resume.set()
        await reuse
        assert worker._jupyter_user_pool.stats()["user"] == {"idle":0,"in_flight":0}
        worker._destroy_sandbox.assert_awaited_once()

    asyncio.run(scenario())


def test_scan_protects_all_idle_candidates_before_batching_and_is_fair(monkeypatch):
    worker = _worker(monkeypatch,None)
    worker._sessions = {
        f"chat-{i}":_Session(sandbox=SimpleNamespace(id=f"sbx-{i}"),
                           last_active_ts=time.monotonic()-7200)
        for i in range(8)
    }
    protected, parked = [], []
    async def renew(sess):
        protected.append(sess.sandbox.id)
    async def park(chat, sess):
        parked.append(chat)
        worker._parking_in_flight.discard(chat)
    worker._renew = renew
    worker._park_session_via_snapshot = park

    async def scenario():
        from tests.sandbox.test_session_affinity import _age
        for chat, sess in worker._sessions.items():
            await session_registry.remember(chat, str(sess.sandbox.id))
            await _age(session_registry.get_ephemeral_state(), chat, 7200)
        for _ in range(3):
            await worker._snapshot_idle_sessions_once()
            await asyncio.sleep(0)
        assert set(protected) == {f"sbx-{i}" for i in range(8)}
        assert set(parked) == {f"chat-{i}" for i in range(8)}

    asyncio.run(scenario())


def test_hard_expiry_covers_backlog_growth():
    worker = OpenSandboxProvider.__new__(OpenSandboxProvider)
    worker._ttl_s=3600
    worker._snapshot_expiry_enabled=True
    worker._snapshot_capacity=20
    worker._snapshot_expiry_grace_s=300
    worker._sessions={}
    assert worker._session_expiry_s() == 5100
    worker._sessions={str(i):None for i in range(40)}
    assert worker._session_expiry_s() == 6360


@pytest.mark.parametrize("response,expected_stale", [
    (httpx.Response(404,json={"code":"DOCKER::SANDBOX_NOT_FOUND"}),True),
    (httpx.Response(404,text="proxy route not found"),False),
    (httpx.Response(401,json={"code":"UNAUTHORIZED"}),False),
    (httpx.Response(500,json={"code":"DOCKER::INTERNAL_ERROR"}),False),
])
def test_only_confirmed_missing_renewal_marks_stale(monkeypatch,response,expected_stale):
    worker=_worker(monkeypatch,response)
    del worker._renew
    worker._ttl_s=3600
    async def scenario():
        sess=await _bind(worker)
        await worker._renew_in_background("chat",sess)
        assert sess.stale_marked is expected_stale
    asyncio.run(scenario())



def test_snapshot_readiness_timeout_bounds_a_blocked_http_request(monkeypatch):
    from core.sandbox.errors import SandboxTimeoutError
    worker=_worker(monkeypatch,None)
    async def blocked(*args,**kwargs):
        await asyncio.Event().wait()
    worker._get_http_client=lambda:SimpleNamespace(get=blocked)
    del worker._wait_snapshot_ready
    with pytest.raises(SandboxTimeoutError):
        asyncio.run(worker._wait_snapshot_ready("snap",timeout_s=0.01))


def test_lost_shared_claim_interrupts_owner_without_deleting_another_token(monkeypatch, sandbox_session_store):
    monkeypatch.setattr(session_registry,"_LOCK_RENEW_INTERVAL_S",0.001)
    original_hold=sandbox_session_store.compare_exchange
    calls=0
    async def hold(key,expected,token,*,ttl):
        nonlocal calls
        calls+=1
        if calls >= 1:
            await sandbox_session_store.drop(key)
            await sandbox_session_store.put(key,"new-owner",ttl=ttl)
            return False
        return await original_hold(key,expected,token,ttl=ttl)
    monkeypatch.setattr(sandbox_session_store,"compare_exchange",hold)
    async def scenario():
        with pytest.raises(RuntimeError,match="claim lost"):
            async with session_registry.claim("chat"):
                await asyncio.Event().wait()
        key=session_registry._LOCK_KEY.format(session_id="chat")
        assert await sandbox_session_store.get(key) == "new-owner"
    asyncio.run(scenario())


def test_lost_claim_after_detach_returns_reservation_without_destroying_container(monkeypatch):
    worker=_worker(monkeypatch,None)
    async def cancelled_forget(*args):
        raise asyncio.CancelledError()
    monkeypatch.setattr(session_registry,"forget",cancelled_forget)
    async def scenario():
        sess=await _bind(worker)
        monkeypatch.setattr(session_registry,"claim_valid",lambda:False)
        with pytest.raises(asyncio.CancelledError):
            await worker._release_session("chat",sess)
        assert worker._jupyter_user_pool.stats()["user"]["in_flight"] == 0
        worker._destroy_sandbox.assert_not_awaited()
    asyncio.run(scenario())


def test_cancelled_resource_stop_preserves_session_and_reservation(monkeypatch):
    from core.capabilities.plugins.resources import prewarm
    worker=_worker(monkeypatch,None)
    monkeypatch.setattr(prewarm,"stop",AsyncMock(side_effect=asyncio.CancelledError()))
    async def scenario():
        sess=await _bind(worker)
        with pytest.raises(asyncio.CancelledError):
            await worker._destroy_session("chat",sess)
        assert worker._sessions["chat"] is sess
        assert worker._jupyter_user_pool.stats()["user"]["in_flight"] == 1
        worker._destroy_sandbox.assert_not_awaited()
    asyncio.run(scenario())


def test_cancellation_while_waiting_for_pool_lock_still_returns_reservation(monkeypatch):
    worker=_worker(monkeypatch,None)
    valid=True
    monkeypatch.setattr(session_registry,"claim_valid",lambda:valid)
    async def scenario():
        nonlocal valid
        sess=await _bind(worker)
        await worker._jupyter_user_pool._lock.acquire()
        release=asyncio.create_task(worker._release_session("chat",sess))
        for _ in range(5):
            await asyncio.sleep(0)
        assert "chat" not in worker._sessions
        valid=False
        release.cancel()
        worker._jupyter_user_pool._lock.release()
        with pytest.raises(asyncio.CancelledError):
            await release
        assert worker._jupyter_user_pool.stats()["user"]["in_flight"] == 0
        worker._destroy_sandbox.assert_not_awaited()
    asyncio.run(scenario())
