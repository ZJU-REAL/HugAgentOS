"""Idle reclamation must back up before it releases, and restore what it backed up.

Two thresholds used to race here: a 600s reaper that wiped /workspace and handed
the container back to the pool, and a 1500s parker that snapshotted first. The
short one always won, so a chat that paused for ten minutes came back to an empty
workspace with no snapshot to restore from. They are now one threshold and one
behaviour, and the restore path prefers the snapshot over a wiped warm container.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.sandbox import _opensandbox_exec as exec_mod
from core.sandbox import _opensandbox_session as session_mod
from core.sandbox._opensandbox_exec import _OpenSandboxExecMixin
from core.sandbox._opensandbox_session import _OpenSandboxSessionMixin


def _settings(**overrides):
    values = {
        "opensandbox_snapshot_enabled": True,
        "idle_ttl_s": 3600,
    }
    values.update(overrides)
    return SimpleNamespace(sandbox=SimpleNamespace(**values))


def _restore_provider(monkeypatch, *, snapshot_id, idle_hit):
    monkeypatch.setattr(session_mod, "settings", _settings())
    monkeypatch.setattr(session_mod, "_user_bound_sandbox_required", lambda: True)
    provider = _OpenSandboxSessionMixin()
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


def test_reaping_a_chat_session_snapshots_before_releasing(monkeypatch):
    monkeypatch.setattr(session_mod, "settings", _settings())
    provider = _OpenSandboxSessionMixin()
    sess = SimpleNamespace(sandbox=SimpleNamespace(id="sbx-1"))
    provider._sessions = {"chat-1": sess}
    provider._parking_in_flight = set()
    provider._get_session_lock = AsyncMock(return_value=asyncio.Lock())
    provider._take_snapshot = AsyncMock(return_value="snap-1")
    provider._wait_snapshot_ready = AsyncMock()
    provider._upsert_snapshot = AsyncMock(return_value=None)
    provider._destroy_session = AsyncMock()

    asyncio.run(provider._park_session_via_snapshot("chat-1", sess))

    provider._take_snapshot.assert_awaited_once_with("sbx-1")
    provider._upsert_snapshot.assert_awaited_once()
    # Released only after the snapshot is Ready and recorded, and handed back to
    # the pool rather than destroyed — the snapshot is what carries the state now.
    provider._destroy_session.assert_awaited_once_with("chat-1", sess, reuse_sandbox=True)


def test_park_scan_uses_the_single_idle_threshold(monkeypatch):
    """No second threshold may undercut the snapshot-then-release path."""
    monkeypatch.setattr(session_mod, "settings", _settings())
    provider = _OpenSandboxSessionMixin()
    provider._parking_in_flight = set()
    now = time.monotonic()
    provider._sessions = {
        "fresh": SimpleNamespace(last_active_ts=now, stale_marked=False),
        "idle": SimpleNamespace(last_active_ts=now - 4000, stale_marked=False),
    }
    provider._park_session_via_snapshot = AsyncMock()

    asyncio.run(provider._snapshot_idle_sessions_once())

    assert provider._parking_in_flight == {"idle"}


def test_stale_idle_sessions_are_dropped_rather_than_parked(monkeypatch):
    """Their sandbox is already gone, so they can only be dropped — but dropped they must be."""
    monkeypatch.setattr(session_mod, "settings", _settings())
    provider = _OpenSandboxSessionMixin()
    provider._parking_in_flight = set()
    sess = SimpleNamespace(last_active_ts=time.monotonic() - 4000, stale_marked=True)
    provider._sessions = {"dead": sess}
    provider._park_session_via_snapshot = AsyncMock()
    provider._destroy_session = AsyncMock()

    asyncio.run(provider._snapshot_idle_sessions_once())

    provider._destroy_session.assert_awaited_once_with("dead", sess)
    provider._park_session_via_snapshot.assert_not_awaited()


def test_pool_reaper_never_touches_chat_sessions(monkeypatch):
    """Chat sandboxes are reclaimed by the snapshot path alone."""
    monkeypatch.setattr(exec_mod, "settings", _settings())
    provider = _OpenSandboxExecMixin()
    provider._sessions = {"chat-1": SimpleNamespace(last_active_ts=0.0)}
    provider._destroy_session = AsyncMock()
    provider._pool = SimpleNamespace(sweep_and_refill=AsyncMock(return_value=0))
    provider._jupyter_user_pool = SimpleNamespace(reap_idle=AsyncMock(return_value=2))

    n = asyncio.run(provider.reap_idle_sessions())

    assert n == 2
    assert "chat-1" in provider._sessions
    provider._destroy_session.assert_not_awaited()
