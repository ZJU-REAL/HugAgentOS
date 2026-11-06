"""Idle reclamation must back up before it releases, and restore what it backed up.

A snapshot is the controller's asynchronous job. The backend records it and
reads its outcome on later scans; no local timer decides it failed.

Two thresholds used to race here: a 600s reaper that wiped /workspace and handed
the container back to the pool, and a 1500s parker that snapshotted first. The
short one always won, so a chat that paused for ten minutes came back to an empty
workspace with no snapshot to restore from. They are now one threshold and one
behaviour, and the restore path prefers the snapshot over a wiped warm container.
"""

from __future__ import annotations

from tests.sandbox.provider_test_support import patch_open_settings

import asyncio
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import core.sandbox.providers.opensandbox.execution as exec_mod
import core.sandbox.providers.opensandbox.runtime as session_mod_runtime
import core.sandbox.sessions.registry as session_registry
from tests.sandbox.test_session_affinity import _age
import core.sandbox.providers.opensandbox.lifecycle as session_mod
from core.sandbox.providers.opensandbox.execution import _OpenSandboxExecMixin
from core.sandbox.providers.opensandbox.provider import OpenSandboxProvider
import core.sandbox.providers.opensandbox.snapshot_store as snapshot_store
from core.infra.time import utc_now
from core.sandbox.errors import SandboxError


def _settings(**overrides):
    values = {
        "opensandbox_snapshot_enabled": True,
        "idle_ttl_s": 3600,
    }
    values.update(overrides)
    return SimpleNamespace(sandbox=SimpleNamespace(**values))


def _restore_provider(monkeypatch, *, snapshot_id, idle_hit):
    monkeypatch.setattr('core.capabilities.skills.publication.prepare_skill_view', lambda uid: None)
    patch_open_settings(monkeypatch, _settings())
    provider = OpenSandboxProvider.__new__(OpenSandboxProvider)
    provider._lookup_snapshot = AsyncMock(return_value=snapshot_id)
    provider._jupyter_user_pool = SimpleNamespace(has_idle=lambda uid: idle_hit)
    provider._create_session = AsyncMock(
        return_value=SimpleNamespace(sandbox=SimpleNamespace(id="warm"))
    )
    provider._create_session_from_snapshot = AsyncMock(
        return_value=SimpleNamespace(sandbox=SimpleNamespace(id="restored"))
    )
    return provider


def test_snapshot_wins_over_a_warm_idle_container(monkeypatch):
    """The pool container is wiped; serving it would drop the chat's files."""
    provider = _restore_provider(monkeypatch, snapshot_id="snap-1", idle_hit=True)

    sess = asyncio.run(provider._create_session_for("chat-1", user_id="user-1"))

    assert sess.sandbox.id == "restored"
    provider._create_session_from_snapshot.assert_awaited_once()
    provider._create_session.assert_not_awaited()


def test_warm_idle_container_is_used_when_there_is_nothing_to_restore(monkeypatch):
    provider = _restore_provider(monkeypatch, snapshot_id=None, idle_hit=True)

    sess = asyncio.run(provider._create_session_for("chat-1", user_id="user-1"))

    assert sess.sandbox.id == "warm"
    provider._create_session.assert_awaited_once()
    provider._create_session_from_snapshot.assert_not_awaited()


class _Controller:
    """Snapshot endpoints of a controller whose snapshots settle when the test says."""

    def __init__(self):
        self.states: dict[str, str] = {}
        self.started: list[str] = []
        self.deleted: list[str] = []
        self.delete_errors = 0

    async def take(self, sandbox_id):
        snapshot_id = f"snap-{len(self.started) + 1}"
        self.started.append(sandbox_id)
        self.states[snapshot_id] = "Creating"
        return snapshot_id

    async def state(self, snapshot_id):
        return self.states.get(snapshot_id, "Missing")

    async def delete(self, snapshot_id):
        if self.delete_errors:
            self.delete_errors -= 1
            raise SandboxError("HTTP 409 SNAPSHOT::DELETE_CONFLICT")
        self.deleted.append(snapshot_id)
        self.states.pop(snapshot_id, None)


def _parker(monkeypatch, chats=("chat-1",)):
    patch_open_settings(monkeypatch, _settings(opensandbox_snapshot_retention_days=7))
    provider = OpenSandboxProvider.__new__(OpenSandboxProvider)
    idle = time.monotonic() - 7200
    provider._sessions = {
        chat: SimpleNamespace(sandbox=SimpleNamespace(id=f"sbx-{chat}"), last_active_ts=idle,
                              stale_marked=False)
        for chat in chats
    }
    locks = {}

    async def get_lock(chat):
        return locks.setdefault(chat, asyncio.Lock())

    async def destroy(chat, sess, reuse_sandbox=False):
        provider._sessions.pop(chat, None)

    controller = _Controller()
    provider._get_session_lock = get_lock
    provider._discover_idle_sessions = AsyncMock()
    provider._renew = AsyncMock()
    provider._destroy_session = AsyncMock(side_effect=destroy)
    provider._take_snapshot = controller.take
    provider._snapshot_state = controller.state
    provider._delete_snapshot = controller.delete
    return provider, controller


async def _idle(*chats):
    for chat in chats:
        await session_registry.remember(chat, f"sbx-{chat}")
        await _age(session_registry.get_ephemeral_state(), chat, 7200)


def _queued():
    return snapshot_store.load_deletions(100)


def _restore_point(chat):
    from core.db.models import ChatSandboxSnapshot
    from core.db.engine import SessionLocal

    with SessionLocal() as db:
        row = db.get(ChatSandboxSnapshot, chat)
        return row.snapshot_id if row else None


def test_a_slow_snapshot_is_followed_until_ready_not_retaken(monkeypatch, provider_database):
    """Production: the backend gave up at 120 s, the controller finished at 149 s.

    Nothing local decides the outcome now: scans keep reading the same job and
    the container is released only once the controller reports Ready.
    """
    provider, controller = _parker(monkeypatch)

    async def scenario():
        await _idle("chat-1")
        await provider._snapshot_idle_sessions_once()
        assert controller.started == ["sbx-chat-1"]
        for _ in range(3):  # minutes pass while the controller works
            await provider._snapshot_idle_sessions_once()
        assert controller.started == ["sbx-chat-1"]
        assert "chat-1" in provider._sessions
        assert _restore_point("chat-1") is None

        controller.states["snap-1"] = "Ready"
        await provider._snapshot_idle_sessions_once()

    asyncio.run(scenario())

    assert _restore_point("chat-1") == "snap-1"
    assert snapshot_store.load_jobs() == {}
    assert controller.deleted == []
    provider._destroy_session.assert_awaited_once()
    assert provider._destroy_session.await_args.kwargs == {"reuse_sandbox": True}


def test_a_failed_snapshot_is_deleted_and_the_chat_backs_off(monkeypatch, provider_database):
    provider, controller = _parker(monkeypatch)

    async def scenario():
        await _idle("chat-1")
        await provider._snapshot_idle_sessions_once()
        controller.states["snap-1"] = "Failed"
        await provider._snapshot_idle_sessions_once()
        await provider._snapshot_idle_sessions_once()
        job = snapshot_store.load_jobs()["chat-1"]
        # Deleted on the controller, so failed records stop piling up there,
        # and no new attempt while the backoff runs.
        assert controller.deleted == ["snap-1"]
        assert (job.state, job.snapshot_id, job.failures) == ("failed", None, 1)
        assert controller.started == ["sbx-chat-1"]
        assert "chat-1" in provider._sessions

        with provider_database() as db:
            from core.db.models import ChatSandboxSnapshotJob
            db.get(ChatSandboxSnapshotJob, "chat-1").retry_after = job.started_at
            db.commit()
        await provider._snapshot_idle_sessions_once()
        assert controller.started == ["sbx-chat-1", "sbx-chat-1"]
        job = snapshot_store.load_jobs()["chat-1"]
        assert (job.state, job.snapshot_id, job.failures) == ("creating", "snap-2", 1)

    asyncio.run(scenario())


def test_backoff_doubles_up_to_the_cap(monkeypatch):
    provider, _ = _parker(monkeypatch)
    now = time.time()
    delays = [provider._snapshot_retry_after(n).timestamp() - now for n in (1, 2, 3, 5, 9)]
    assert [round(d, -1) for d in delays] == [300, 600, 1200, 3600, 3600]


def test_a_snapshot_of_a_chat_used_meanwhile_is_discarded(monkeypatch, provider_database):
    provider, controller = _parker(monkeypatch)

    async def scenario():
        await _idle("chat-1")
        await provider._snapshot_idle_sessions_once()
        await asyncio.sleep(0.01)
        await session_registry.remember("chat-1", "sbx-chat-1")  # the user came back
        controller.states["snap-1"] = "Ready"
        await provider._snapshot_idle_sessions_once()

    asyncio.run(scenario())

    assert controller.deleted == ["snap-1"]
    assert _restore_point("chat-1") is None
    assert snapshot_store.load_jobs() == {}
    assert "chat-1" in provider._sessions
    provider._destroy_session.assert_not_awaited()


def test_snapshots_in_flight_are_bounded_across_scans(monkeypatch, provider_database):
    chats = ("chat-1", "chat-2", "chat-3")
    provider, controller = _parker(monkeypatch, chats)

    async def scenario():
        await _idle(*chats)
        await provider._snapshot_idle_sessions_once()
        await provider._snapshot_idle_sessions_once()
        assert len(controller.started) == provider._SNAPSHOT_CONCURRENCY
        controller.states["snap-1"] = "Ready"
        await provider._snapshot_idle_sessions_once()

    asyncio.run(scenario())

    assert len(controller.started) == 3
    assert {job.state for job in snapshot_store.load_jobs().values()} == {"creating"}


def test_a_failed_remote_delete_is_retried(monkeypatch, provider_database):
    provider, controller = _parker(monkeypatch)
    controller.delete_errors = 1

    async def scenario():
        await _idle("chat-1")
        await provider._snapshot_idle_sessions_once()
        controller.states["snap-1"] = "Failed"
        await provider._snapshot_idle_sessions_once()
        assert _queued() == ["snap-1"]
        await provider._snapshot_idle_sessions_once()

    asyncio.run(scenario())

    assert controller.deleted == ["snap-1"]
    assert _queued() == []


def test_a_delete_the_controller_keeps_refusing_never_blocks_the_chat(
        monkeypatch, provider_database):
    """Deletion and the next attempt are separate books: a refused delete of
    the failed snapshot must not keep the container from ever being parked."""
    from core.db.models import ChatSandboxSnapshotJob

    provider, controller = _parker(monkeypatch)
    controller.delete_errors = 10**6

    async def scenario():
        await _idle("chat-1")
        await provider._snapshot_idle_sessions_once()
        controller.states["snap-1"] = "Failed"
        await provider._snapshot_idle_sessions_once()
        with provider_database() as db:
            db.get(ChatSandboxSnapshotJob, "chat-1").retry_after = utc_now()
            db.commit()
        await provider._snapshot_idle_sessions_once()
        controller.states["snap-2"] = "Ready"
        await provider._snapshot_idle_sessions_once()

    asyncio.run(scenario())

    assert _restore_point("chat-1") == "snap-2"
    assert _queued() == ["snap-1"]
    assert "chat-1" not in provider._sessions


def test_promotion_replaces_and_deletes_the_previous_restore_point(monkeypatch, provider_database):
    from core.db.models import ChatSandboxSnapshot

    provider, controller = _parker(monkeypatch)
    with provider_database() as db:
        db.add(ChatSandboxSnapshot(chat_id="chat-1", snapshot_id="snap-old", sandbox_id="old",
                                   expires_at=utc_now() + timedelta(days=1)))
        db.commit()

    async def scenario():
        await _idle("chat-1")
        await provider._snapshot_idle_sessions_once()
        controller.states["snap-1"] = "Ready"
        await provider._snapshot_idle_sessions_once()

    asyncio.run(scenario())

    assert _restore_point("chat-1") == "snap-1"
    assert controller.deleted == ["snap-old"]


def test_a_job_whose_sandbox_is_gone_is_discarded_once_it_settles(monkeypatch, provider_database):
    provider, controller = _parker(monkeypatch)

    async def scenario():
        await _idle("chat-1")
        await provider._snapshot_idle_sessions_once()
        provider._sessions.clear()
        await session_registry.remember("chat-1", "replacement")
        controller.states["snap-1"] = "Ready"
        await provider._snapshot_idle_sessions_once()

    asyncio.run(scenario())

    assert controller.deleted == ["snap-1"]
    assert snapshot_store.load_jobs() == {}
    assert _restore_point("chat-1") is None


def test_a_job_the_controller_never_settles_frees_its_slot(monkeypatch, provider_database):
    """A controller restart can leave a snapshot Creating forever; it must not
    hold one of the two slots and stop all reclamation."""
    from core.db.models import ChatSandboxSnapshotJob

    chats = ("chat-1", "chat-2", "chat-3")
    provider, controller = _parker(monkeypatch, chats)
    controller.delete_errors = 1  # the controller refuses to delete a Creating snapshot

    async def scenario():
        await _idle(*chats)
        await provider._snapshot_idle_sessions_once()
        assert len(controller.started) == 2
        stuck = next(iter(snapshot_store.load_jobs().values()))
        with provider_database() as db:
            row = db.get(ChatSandboxSnapshotJob, stuck.chat_id)
            row.started_at = utc_now() - timedelta(seconds=provider._SNAPSHOT_SETTLE_DEADLINE_S + 1)
            db.commit()
        await provider._snapshot_idle_sessions_once()
        return stuck

    stuck = asyncio.run(scenario())

    jobs = snapshot_store.load_jobs()
    assert len(controller.started) == 3  # the slot was handed to the waiting chat
    assert jobs[stuck.chat_id].state == "failed"
    assert _queued() == [stuck.snapshot_id]  # refused once; retried next scan


def test_a_start_after_a_leadership_handover_rereads_the_job(monkeypatch, provider_database):
    """The scan's job list can be stale; the row under the claim decides."""
    provider, controller = _parker(monkeypatch)

    async def scenario():
        await _idle("chat-1")
        await asyncio.to_thread(
            snapshot_store.record_creating, "chat-1", sandbox_id="sbx-chat-1",
            snapshot_id="snap-other-leader", started_at=utc_now(), failures=0,
        )
        await provider._start_snapshot("chat-1", provider._sessions["chat-1"])

    asyncio.run(scenario())

    assert controller.started == []
    assert snapshot_store.load_jobs()["chat-1"].snapshot_id == "snap-other-leader"


def test_an_accepted_snapshot_is_recorded_even_if_the_start_is_cancelled(
        monkeypatch, provider_database):
    provider, controller = _parker(monkeypatch)
    accepted = asyncio.Event()
    release = asyncio.Event()

    async def slow_take(sandbox_id):
        accepted.set()
        await release.wait()
        return await controller.take(sandbox_id)

    provider._take_snapshot = slow_take

    async def scenario():
        await _idle("chat-1")
        task = asyncio.create_task(provider._start_snapshot("chat-1", provider._sessions["chat-1"]))
        await accepted.wait()
        task.cancel()  # leadership lost while the controller is answering
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        for _ in range(20):
            if snapshot_store.load_jobs():
                break
            await asyncio.sleep(0.05)

    asyncio.run(scenario())

    assert snapshot_store.load_jobs()["chat-1"].snapshot_id == "snap-1"


def test_a_replaced_restore_point_that_fails_to_delete_stays_tracked(
        monkeypatch, provider_database):
    from core.db.models import ChatSandboxSnapshot

    provider, controller = _parker(monkeypatch)
    with provider_database() as db:
        db.add(ChatSandboxSnapshot(chat_id="chat-1", snapshot_id="snap-old", sandbox_id="old",
                                   expires_at=utc_now() + timedelta(days=1)))
        db.commit()

    async def scenario():
        await _idle("chat-1")
        await provider._snapshot_idle_sessions_once()
        controller.states["snap-1"] = "Ready"
        controller.delete_errors = 1
        await provider._snapshot_idle_sessions_once()
        assert _queued() == ["snap-old"]
        assert snapshot_store.load_jobs() == {}
        await provider._snapshot_idle_sessions_once()

    asyncio.run(scenario())

    assert _restore_point("chat-1") == "snap-1"
    assert controller.deleted == ["snap-old"]
    assert _queued() == []


def test_use_just_before_the_start_counts_against_clock_skew(monkeypatch, provider_database):
    provider, controller = _parker(monkeypatch)

    async def scenario():
        await _idle("chat-1")
        await provider._snapshot_idle_sessions_once()
        # Another host stamped a use a few seconds "before" the start.
        key = session_registry._KEY.format(session_id="chat-1")
        store = session_registry.get_ephemeral_state()
        entry = __import__("json").loads(await store.get(key))
        entry["active_at"] = snapshot_store.load_jobs()["chat-1"].started_at.timestamp() - 5
        await store.put(key, __import__("json").dumps(entry), ttl=7200)
        controller.states["snap-1"] = "Ready"
        await provider._snapshot_idle_sessions_once()

    asyncio.run(scenario())

    assert controller.deleted == ["snap-1"]
    assert _restore_point("chat-1") is None


def test_used_since_reads_the_shared_last_use(sandbox_session_store):
    async def scenario():
        await session_registry.remember("chat-1", "sbx")
        before = time.time() - 1
        after = time.time() + 1
        missing = await session_registry.used_since("other", before)
        return await session_registry.used_since("chat-1", before), \
            await session_registry.used_since("chat-1", after), missing

    assert asyncio.run(scenario()) == (True, False, True)


def test_park_scan_uses_the_single_idle_threshold(monkeypatch, provider_database):
    """No second threshold may undercut the snapshot-then-release path."""
    provider, controller = _parker(monkeypatch, ("fresh", "idle"))
    provider._sessions["fresh"].last_active_ts = time.monotonic()

    async def scenario():
        await _idle("idle")
        await session_registry.remember("fresh", "sbx-fresh")
        await provider._snapshot_idle_sessions_once()

    asyncio.run(scenario())

    assert controller.started == ["sbx-idle"]


def test_stale_idle_sessions_still_require_snapshot_or_confirmed_disappearance(
        monkeypatch, provider_database):
    """A stale hint is not proof that a live container can be destroyed."""
    provider, controller = _parker(monkeypatch, ("dead",))
    provider._sessions["dead"].stale_marked = True

    async def scenario():
        await _idle("dead")
        await provider._snapshot_idle_sessions_once()

    asyncio.run(scenario())

    provider._destroy_session.assert_not_awaited()
    assert controller.started == ["sbx-dead"]


def test_pool_reaper_never_touches_chat_sessions(monkeypatch):
    """Chat sandboxes are reclaimed by the snapshot path alone."""
    monkeypatch.setattr(exec_mod, "settings", _settings())
    provider = _OpenSandboxExecMixin()
    provider._sessions = {"chat-1": SimpleNamespace(last_active_ts=0.0)}
    provider._destroy_session = AsyncMock()
    provider._reconcile_sessions = AsyncMock(return_value=0)
    provider._pool = SimpleNamespace(sweep_and_refill=AsyncMock(return_value=0))
    provider._jupyter_user_pool = SimpleNamespace(reap_idle=AsyncMock(return_value=2))

    n = asyncio.run(provider.reap_idle_sessions())

    assert n == 2
    assert "chat-1" in provider._sessions
    provider._destroy_session.assert_not_awaited()
