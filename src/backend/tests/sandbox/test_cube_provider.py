"""Behavioral regression coverage."""

from __future__ import annotations
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock
import pytest

from tests.sandbox.provider_test_support import (
    _install_fake_e2b,
    _fake_sbx,
    _reload_cube,
    _bash_req,
    _fake_listed,
    _fake_paginator,
)

pytestmark = pytest.mark.usefixtures("provider_database")


def test_factory_cube_selected(monkeypatch):
    _install_fake_e2b(monkeypatch)
    f = _reload_cube(monkeypatch)
    f.reset_provider_cache()
    p = f.get_sandbox_provider()
    assert p.name == "cube"
    assert type(p).__name__ == "SessionSandboxRouter"
    assert type(p._ordinary).__name__ == "CubeSandboxProvider"
    assert p._for("ordinary-chat") is p._ordinary


def test_cube_missing_template_raises(monkeypatch):
    _install_fake_e2b(monkeypatch)
    f = _reload_cube(monkeypatch, CUBE_TEMPLATE="")
    f.reset_provider_cache()
    from core.sandbox import SandboxError

    with pytest.raises(SandboxError, match="CUBE_TEMPLATE"):
        f.get_sandbox_provider()


def test_cube_managed_command_keeps_session_for_followup(monkeypatch):
    asb, _, _ = _install_fake_e2b(monkeypatch)
    fake = _fake_sbx(stdout="hello\n", exit_code=0)
    asb.create = AsyncMock(return_value=fake)
    f = _reload_cube(monkeypatch)
    f.reset_provider_cache()
    p = f.get_sandbox_provider()
    r = asyncio.run(p.run_to_completion(_bash_req(script_content="echo hello")))
    assert r.stdout == "hello\n"
    assert r.exit_code == 0
    asb.create.assert_awaited_once()
    fake.kill.assert_not_awaited()  # conversation workspace remains available


def test_cube_nonzero_exit_captured_not_raised(monkeypatch):
    """commands.run raises CommandExitException on non-zero exit — provider must
    capture stdout/stderr/exit_code (so bash tool reports failures), not error."""
    asb, _, CEE = _install_fake_e2b(monkeypatch)
    fake = _fake_sbx(run_exc=CEE(stdout="out\n", stderr="err\n", exit_code=3))
    asb.create = AsyncMock(return_value=fake)
    f = _reload_cube(monkeypatch)
    f.reset_provider_cache()
    p = f.get_sandbox_provider()
    r = asyncio.run(p.run_to_completion(_bash_req(script_content="exit 3")))
    assert r.exit_code == 3
    assert r.stdout == "out\n"
    assert r.stderr == "err\n"


def test_cube_timeout_maps_to_timeout_error(monkeypatch):
    asb, TE, _ = _install_fake_e2b(monkeypatch)
    fake = _fake_sbx(run_exc=TE("context deadline exceeded"))
    asb.create = AsyncMock(return_value=fake)
    f = _reload_cube(monkeypatch)
    f.reset_provider_cache()
    from core.sandbox import SandboxError

    p = f.get_sandbox_provider()
    with pytest.raises(SandboxError, match="deadline"):
        asyncio.run(p.run_to_completion(_bash_req(script_content="sleep 99", timeout=1)))


def test_cube_persistent_reuses_sandbox(monkeypatch):
    asb, _, _ = _install_fake_e2b(monkeypatch)
    fake = _fake_sbx(sandbox_id="sbx-persist")
    asb.create = AsyncMock(return_value=fake)
    asb.connect = AsyncMock(return_value=fake)
    f = _reload_cube(monkeypatch)
    f.reset_provider_cache()
    p = f.get_sandbox_provider()

    async def _two():
        await p.run_to_completion(_bash_req(session_id="chat-1"))
        await p.run_to_completion(_bash_req(session_id="chat-1"))
        return await p.current_sandbox_id("chat-1")

    sid = asyncio.run(_two())
    assert sid == "sbx-persist"
    asb.create.assert_awaited_once()  # created once for the session
    asb.connect.assert_awaited()  # reconnected on the 2nd call
    fake.kill.assert_not_awaited()  # persistent session must NOT be killed


def test_cube_second_worker_attaches_to_the_bound_sandbox(monkeypatch):
    """cube 也走共享登记：换个进程服务同一个会话，必须落回同一台容器。

    这条和 opensandbox 那组（tests/sandbox/test_session_affinity.py）是同一个不变量
    ——绑定不能只活在某个 worker 的内存里，否则同一会话的产物会散在两台容器上。
    """
    asb, _, _ = _install_fake_e2b(monkeypatch)
    fake = _fake_sbx(sandbox_id="sbx-shared")
    asb.create = AsyncMock(return_value=fake)
    asb.connect = AsyncMock(return_value=fake)
    f = _reload_cube(monkeypatch)
    f.reset_provider_cache()
    first = f.get_sandbox_provider()

    async def _across_workers():
        await first.run_to_completion(_bash_req(session_id="chat-1"))
        # 另一个 worker：全新的 provider 实例，进程内什么都不知道，只共用登记
        f.reset_provider_cache()
        second = f.get_sandbox_provider()
        await second.run_to_completion(_bash_req(session_id="chat-1"))
        return await second.current_sandbox_id("chat-1")

    try:
        assert asyncio.run(_across_workers()) == "sbx-shared"
        asb.create.assert_awaited_once()  # 第二个 worker 接管而不是另建一台
    finally:
        # 这里比别的用例多建了一个 provider，不收掉会被下一个用例捡去用
        f.reset_provider_cache()


def test_cube_current_sandbox_id_is_pure_query(monkeypatch):
    asb, _, _ = _install_fake_e2b(monkeypatch)
    asb.create = AsyncMock()
    asb.connect = AsyncMock()
    f = _reload_cube(monkeypatch)
    f.reset_provider_cache()
    p = f.get_sandbox_provider()
    assert asyncio.run(p.current_sandbox_id("unknown")) is None
    assert asyncio.run(p.current_sandbox_id(None)) is None
    asb.create.assert_not_awaited()  # MUST NOT create/connect a sandbox
    asb.connect.assert_not_awaited()


def test_cube_pool_disabled_no_prewarm(monkeypatch):
    """min_idle=0: warmup / refill are both no-ops, zero creations."""
    asb, _, _ = _install_fake_e2b(monkeypatch)
    asb.create = AsyncMock(return_value=_fake_sbx())
    f = _reload_cube(monkeypatch, CUBE_POOL_MIN_IDLE="0")
    f.reset_provider_cache()
    p = f.get_sandbox_provider()

    async def go():
        await p.warmup()
        await p._refill_pool()

    asyncio.run(go())
    assert asb.create.await_count == 0
    assert len(p._idle_pool) == 0


def test_cube_refill_fills_to_min_idle(monkeypatch):
    """refill fills the idle pool up to min_idle, and the pool count is correct after concurrent creation."""
    asb, _, _ = _install_fake_e2b(monkeypatch)
    asb.create = AsyncMock(side_effect=lambda *a, **k: _fake_sbx(sandbox_id="warm"))
    f = _reload_cube(monkeypatch, CUBE_POOL_MIN_IDLE="2")
    f.reset_provider_cache()
    p = f.get_sandbox_provider()

    async def go():
        await p._refill_pool()
        return len(p._idle_pool)

    assert asyncio.run(go()) == 2
    assert asb.create.await_count == 2


def test_cube_acquire_hits_pool_skips_cold_start(monkeypatch):
    """When the pool has a fresh-enough warm sandbox, _acquire_warm returns it directly instead of creating on the spot."""
    asb, _, _ = _install_fake_e2b(monkeypatch)
    fresh = _fake_sbx(sandbox_id="fresh")  # an on-the-spot creation would get this one
    asb.create = AsyncMock(return_value=fresh)
    f = _reload_cube(monkeypatch, CUBE_POOL_MIN_IDLE="1")
    f.reset_provider_cache()
    p = f.get_sandbox_provider()

    warm = _fake_sbx(sandbox_id="warm-1")

    async def go():
        p._idle_pool.append((warm, time.monotonic()))  # pre-seed a fresh warm one
        got = await p._acquire_warm(None)
        if p._pool_refill_task:  # wait for the refill to finish, avoiding pending warnings
            await p._pool_refill_task
        return got

    got = asyncio.run(go())
    assert got is warm  # what we got is from the pool, not the on-the-spot-created fresh
    assert got.sandbox_id == "warm-1"


def test_cube_acquire_discards_aged_entry(monkeypatch):
    """A warm sandbox in the pool that is over-aged (near TTL) is discarded; a new one is created on the spot instead."""
    asb, _, _ = _install_fake_e2b(monkeypatch)
    fresh = _fake_sbx(sandbox_id="fresh")
    asb.create = AsyncMock(return_value=fresh)
    f = _reload_cube(monkeypatch, CUBE_POOL_MIN_IDLE="1")
    f.reset_provider_cache()
    p = f.get_sandbox_provider()

    old = _fake_sbx(sandbox_id="old")

    async def go():
        # born set to long ago -> exceeds _pool_entry_max_age -> discarded
        p._idle_pool.append((old, time.monotonic() - 10**9))
        got = await p._acquire_warm(None)
        if p._pool_refill_task:
            await p._pool_refill_task
        return got

    got = asyncio.run(go())
    assert got is fresh  # the over-aged one was skipped, a new one was created on the spot


def test_cube_reap_returns_count_and_maintains_pool(monkeypatch):
    """reap_idle_sessions returns the reap count (fixing the old flaw of returning None), and refills the pool along the way."""
    asb, _, _ = _install_fake_e2b(monkeypatch)
    asb.create = AsyncMock(side_effect=lambda *a, **k: _fake_sbx(sandbox_id="warm"))
    asb.connect = AsyncMock(return_value=_fake_sbx())
    f = _reload_cube(monkeypatch, CUBE_POOL_MIN_IDLE="1")
    f.reset_provider_cache()
    p = f.get_sandbox_provider()

    async def go():
        n = (
            await p.reap_idle_sessions()
        )  # no sessions bound -> nothing reaped, but the pool must be refilled
        if p._pool_refill_task:
            await p._pool_refill_task
        return n, len(p._idle_pool)

    n, idle = asyncio.run(go())
    assert n == 0  # no sessions to reap
    assert idle == 1  # pool refilled to min_idle


def test_cube_create_metadata_carries_owner_tag(monkeypatch):
    """With CUBE_OWNER_TAG set, create's metadata['hugagent-owner'] uses that tag."""
    asb, _, _ = _install_fake_e2b(monkeypatch)
    fake = _fake_sbx()
    asb.create = AsyncMock(return_value=fake)
    f = _reload_cube(monkeypatch, CUBE_OWNER_TAG="hugagent-test", CUBE_POOL_MIN_IDLE="0")
    f.reset_provider_cache()
    p = f.get_sandbox_provider()
    asyncio.run(p.run_to_completion(_bash_req(script_content="echo hi")))
    _, kwargs = asb.create.call_args
    assert kwargs["metadata"]["hugagent-owner"] == "hugagent-test"


def test_cube_reconcile_skipped_without_owner_tag(monkeypatch):
    """Without an owner tag -> no list, no sandbox killed (safe default for shared nodes)."""
    asb, _, _ = _install_fake_e2b(monkeypatch)
    asb.list = MagicMock(return_value=_fake_paginator([_fake_listed("x", "backend")]))
    f = _reload_cube(monkeypatch)  # no CUBE_OWNER_TAG
    f.reset_provider_cache()
    p = f.get_sandbox_provider()
    assert asyncio.run(p._reconcile_orphans()) == 0
    asb.list.assert_not_called()


def test_cube_reconcile_kills_only_own_orphans(monkeypatch):
    """Kill only sandboxes with owner==this environment AND not in the registry; other environments (other owner) and known sandboxes are all kept."""
    asb, _, _ = _install_fake_e2b(monkeypatch)
    listed = [
        _fake_listed("orphan-1", "hugagent-test"),  # this environment's orphan -> kill
        _fake_listed("known-1", "hugagent-test"),  # this environment but in the pool -> keep
        _fake_listed("other-1", "hugagent-test"),  # other environment -> keep
    ]
    asb.list = MagicMock(return_value=_fake_paginator(listed))
    asb.connect = AsyncMock(return_value=_fake_sbx(sandbox_id="orphan-1"))
    f = _reload_cube(monkeypatch, CUBE_OWNER_TAG="hugagent-test")
    f.reset_provider_cache()
    p = f.get_sandbox_provider()
    p._idle_pool.append((_fake_sbx(sandbox_id="known-1"), time.monotonic()))  # register known-1

    killed = asyncio.run(p._reconcile_orphans())
    assert killed == 1
    asb.connect.assert_awaited_once()
    assert asb.connect.await_args.args[0] == "orphan-1"  # only connected to the orphan one
