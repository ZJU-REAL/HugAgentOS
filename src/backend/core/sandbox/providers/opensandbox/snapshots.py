"""OpenSandbox session/snapshot/creation methods (mixin)."""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

import core.sandbox.sessions.registry as session_registry
from core.config.settings import settings
from core.sandbox.errors import SandboxNotFoundError, SandboxError, SandboxTimeoutError
from core.sandbox.providers.opensandbox.models import SandboxSessionRecord as _Session

# Snapshot persistence (see internal design docs). Imports are
# deferred to runtime to avoid cross-module circular dependencies at startup
# (models.py also imports core.db.engine).
from core.sandbox.providers.opensandbox.runtime import logger
from core.sandbox.providers.opensandbox.snapshot_store import (
    _SNAPSHOT_STATE_READY,
    _SNAPSHOT_TERMINAL_FAILURE_STATES,
    _get_db_session_local,
    _get_snapshot_model,
)


class _OpenSandboxSnapshotsMixin:
    async def _lookup_snapshot(self, chat_id: str) -> Optional[str]:
        """Query chat_sandbox_snapshots: return the unexpired snapshot_id or None.

        The DB call runs in a thread pool (sync SQLAlchemy) and does not block the event loop.
        """
        from datetime import datetime
        from datetime import timezone as _tz

        def _query() -> Optional[str]:
            SessionLocal = _get_db_session_local()
            ChatSandboxSnapshot = _get_snapshot_model()
            with SessionLocal() as db:
                row = (
                    db.query(ChatSandboxSnapshot)
                    .filter(ChatSandboxSnapshot.chat_id == chat_id)
                    .filter(ChatSandboxSnapshot.expires_at > datetime.now(_tz.utc))
                    .one_or_none()
                )
                return row.snapshot_id if row else None

        return await asyncio.to_thread(_query)

    async def _upsert_snapshot(
        self,
        chat_id: str,
        *,
        snapshot_id: str,
        sandbox_id: str,
        retention_days: Optional[int] = None,
    ) -> Optional[str]:
        """Write to the DB after a successful park. Returns the replaced old snapshot_id (if any) —
        the caller is responsible for asynchronously deleting the old remote snapshot to avoid unbounded accumulation.
        """
        from datetime import datetime
        from datetime import timedelta as _td
        from datetime import timezone as _tz

        if retention_days is None:
            retention_days = settings.sandbox.opensandbox_snapshot_retention_days
        expires_at = datetime.now(_tz.utc) + _td(days=retention_days)

        def _upsert() -> Optional[str]:
            SessionLocal = _get_db_session_local()
            ChatSandboxSnapshot = _get_snapshot_model()
            with SessionLocal() as db:
                old = (
                    db.query(ChatSandboxSnapshot)
                    .filter(ChatSandboxSnapshot.chat_id == chat_id)
                    .one_or_none()
                )
                old_snap_id = old.snapshot_id if old else None
                if old is not None:
                    old.snapshot_id = snapshot_id
                    old.sandbox_id = sandbox_id
                    old.created_at = datetime.now(_tz.utc)
                    old.expires_at = expires_at
                else:
                    db.add(
                        ChatSandboxSnapshot(
                            chat_id=chat_id,
                            snapshot_id=snapshot_id,
                            sandbox_id=sandbox_id,
                            expires_at=expires_at,
                        )
                    )
                db.commit()
                return old_snap_id

        return await asyncio.to_thread(_upsert)

    async def _delete_snapshot_row(self, chat_id: str, snapshot_id: str) -> None:
        """Delete the chat_sandbox_snapshots row (leaves the remote snapshot untouched)."""

        def _delete() -> None:
            SessionLocal = _get_db_session_local()
            ChatSandboxSnapshot = _get_snapshot_model()
            with SessionLocal() as db:
                db.query(ChatSandboxSnapshot).filter(
                    ChatSandboxSnapshot.chat_id == chat_id,
                    ChatSandboxSnapshot.snapshot_id == snapshot_id,
                ).delete()
                db.commit()

        await asyncio.to_thread(_delete)

    async def _defer_snapshot_cleanup(self, chat_id: str, snapshot_id: str) -> None:
        """The snapshot has been consumed by a restore (used as the image to start a new sandbox) → **only move the DB expires_at
        to 1 hour from now**; do not DELETE the remote directly.

        Key timing: the snapshot was just used by ``Sandbox.create(snapshot_id=...)``
        to start a new container, and opensandbox treats the snapshot image as
        in-use (the new container still holds that layer); an immediate DELETE is
        refused with ``HTTP 409 SNAPSHOT::DELETE_CONFLICT`` (confirmed in practice).

        Correct strategy: move this DB row's expires_at to 1 hour out; the GC worker
        will scan it within the hour, by which time the new sandbox is either still
        running (the snapshot still has a reference, GC also gets 409 → retry next
        round) or has been parked and replaced by a new snapshot (the old snapshot
        has no references left, DELETE succeeds). Worst case falls back to forced
        cleanup after retention (7 days) — still more aggressive than starting at
        7 days.

        Fire-and-forget; failures are only logged.
        """
        from datetime import datetime
        from datetime import timedelta as _td
        from datetime import timezone as _tz

        def _mark_short_retention() -> None:
            SessionLocal = _get_db_session_local()
            ChatSandboxSnapshot = _get_snapshot_model()
            with SessionLocal() as db:
                row = (
                    db.query(ChatSandboxSnapshot)
                    .filter(ChatSandboxSnapshot.chat_id == chat_id)
                    .filter(ChatSandboxSnapshot.snapshot_id == snapshot_id)
                    .one_or_none()
                )
                if row is None:
                    return  # already overwritten by a newer park (snapshot_id mismatch) → the old row is gone, nothing to do
                row.expires_at = datetime.now(_tz.utc) + _td(hours=1)
                db.commit()

        try:
            await asyncio.to_thread(_mark_short_retention)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[opensandbox] mark used snapshot=%s for short retention failed: %s",
                snapshot_id,
                e,
            )

    _PARK_BATCH_LIMIT = 3

    _SNAPSHOT_IDLE_LOOP_INTERVAL_S = 60

    _SNAPSHOT_GC_LOOP_INTERVAL_S = 3600

    _GC_DELETE_CONCURRENCY = 5

    async def _periodic_until_shutdown(
        self,
        name: str,
        fn: Callable[[], Awaitable[None]],
        interval_s: int,
    ) -> None:
        """Run ``fn()`` every ``interval_s`` seconds until ``_snapshot_workers_shutdown`` is set.

        Exceptions only warn without exiting; CancelledError propagates."""
        while not self._snapshot_workers_shutdown.is_set():
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("[opensandbox] %s loop iteration failed: %s", name, e)
            try:
                await asyncio.wait_for(
                    self._snapshot_workers_shutdown.wait(),
                    timeout=interval_s,
                )
                break  # shutdown signaled
            except asyncio.TimeoutError:
                pass  # interval elapsed, continue to the next round

    async def _snapshot_idle_sessions_loop(self) -> None:
        """Scan every 60s: snapshot then release sessions idle > IDLE_REAP_S.

        This is the only path that reclaims a chat-bound session, so the
        snapshot is never skipped — the workspace is always restorable on the
        chat's next turn.
        """
        logger.info(
            "[opensandbox] snapshot_idle worker started " "(threshold=%ds, interval=%ds)",
            settings.sandbox.idle_ttl_s,
            self._SNAPSHOT_IDLE_LOOP_INTERVAL_S,
        )
        await self._periodic_until_shutdown(
            "snapshot_idle",
            self._snapshot_idle_sessions_once,
            self._SNAPSHOT_IDLE_LOOP_INTERVAL_S,
        )

    async def _snapshot_idle_sessions_once(self) -> None:
        """Scan _sessions once, find sessions idle long enough + not already parking in-flight,
        and park them asynchronously. Each round triggers at most _PARK_BATCH_LIMIT to avoid docker commit congestion.

        The idle threshold starts snapshotting; server expiry includes a grace
        period so that the scan and snapshot can finish before container removal.
        """
        await self._discover_idle_sessions()
        threshold = settings.sandbox.idle_ttl_s
        now = time.monotonic()
        candidates: list[tuple[str, _Session]] = []
        entries = list(self._sessions.items())
        await self._protect_queued_snapshots(entries)
        offset = getattr(self, "_snapshot_scan_offset", 0) % max(1, len(entries))
        entries = entries[offset:] + entries[:offset]
        for index, (chat_id, sess) in enumerate(entries):
            self._snapshot_scan_offset = offset + index + 1
            if chat_id in self._parking_in_flight:
                continue
            if now - sess.last_active_ts < threshold:
                continue
            # 本进程闲着，不代表这个会话闲着：多 worker 下相邻两轮可能都由别人服务。
            # 回收是「快照 + 销毁容器」，按局部视角判断会把别人正在用的容器收掉。
            if await session_registry.in_use_elsewhere(chat_id, threshold):
                continue
            candidates.append((chat_id, sess))
            if len(candidates) >= self._PARK_BATCH_LIMIT:
                break
        for chat_id, _ in candidates:
            self._parking_in_flight.add(chat_id)
        # Children belong to the maintenance task: losing leadership cancels
        # and joins every in-flight snapshot before releasing ownership.
        async with asyncio.TaskGroup() as group:
            for chat_id, sess in candidates:
                group.create_task(self._park_session_via_snapshot(chat_id, sess))

    async def _park_session_via_snapshot(self, chat_id: str, sess: _Session) -> None:
        """take snapshot → wait Ready → upsert DB → release the session.

        The release passes ``reuse_sandbox=True``: the snapshot already holds
        this chat's workspace, so the wiped container is worth handing back to
        the user's idle pool for the warm path. Restores come from the
        snapshot, which ``_create_session_for`` prefers over an idle hit.

        A confirmed missing sandbox retires its session and reservation.
        Other failures keep the session so that the next cycle can retry.
        """
        sandbox_id = sess.sandbox.id
        try:
            # Idempotency guard inside the lock: failing to take the lock = someone else is using it, skip this attempt
            lock = await self._get_session_lock(chat_id)
            if lock.locked():
                logger.info(
                    "[opensandbox] park chat=%s skipped: session lock held",
                    chat_id,
                )
                return
            async with lock, session_registry.claim(chat_id):
                # double-check: the session may have been replaced/destroyed in the instant of taking the lock
                cur = self._sessions.get(chat_id)
                if cur is not sess:
                    return
                if await session_registry.lookup(chat_id) != str(sandbox_id):
                    await self._release_session(chat_id, sess, detach_only=True)
                    return
                # 拿到跨进程认领后再确认一次全局空闲：等锁期间别的 worker 可能刚服务过它。
                if await session_registry.in_use_elsewhere(
                    chat_id, settings.sandbox.idle_ttl_s
                ):
                    logger.info(
                        "[opensandbox] park chat=%s skipped: still in use elsewhere", chat_id,
                    )
                    return
                try:
                    await self._renew(sess)
                    await session_registry.renew_binding(chat_id, str(sess.sandbox.id))
                except Exception as exc:
                    logger.warning("[opensandbox] pre-snapshot renewal failed sandbox=%s: %s",
                                   sandbox_id, exc)
                try:
                    snap_id = await self._take_snapshot(sandbox_id)
                except SandboxNotFoundError:
                    # Still under the session lock and shared claim. Do not
                    # reuse a missing container or charge its reservation forever.
                    sess.stale_marked = True
                    await self._destroy_session(chat_id, sess)
                    logger.warning(
                        "[opensandbox] retired missing sandbox=%s session=%s",
                        sandbox_id, chat_id,
                    )
                    return
                await self._wait_snapshot_ready(snap_id)
                old_snap = await self._upsert_snapshot(
                    chat_id, snapshot_id=snap_id, sandbox_id=sandbox_id,
                )
                # The actual release; the container goes back to the idle pool
                await self._destroy_session(chat_id, sess, reuse_sandbox=True)
                logger.info(
                    "[opensandbox] parked chat=%s snapshot=%s (replaced_old=%s)",
                    chat_id, snap_id, old_snap or "none",
                )
            # Clean up the old remote snapshot outside the lock (async)
            if old_snap:
                await self._delete_snapshot(old_snap)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[opensandbox] park chat=%s sandbox=%s failed: %s",
                chat_id, sandbox_id, e,
            )
        finally:
            self._parking_in_flight.discard(chat_id)

    async def _snapshot_gc_loop(self) -> None:
        """Scan hourly: delete expired snapshots (DB rows + opensandbox remote)."""
        logger.info(
            "[opensandbox] snapshot_gc worker started " "(retention=%dd, interval=%ds)",
            settings.sandbox.opensandbox_snapshot_retention_days,
            self._SNAPSHOT_GC_LOOP_INTERVAL_S,
        )
        await self._periodic_until_shutdown(
            "snapshot_gc",
            self._snapshot_gc_once,
            self._SNAPSHOT_GC_LOOP_INTERVAL_S,
        )

    async def _snapshot_gc_once(self) -> None:
        from datetime import datetime
        from datetime import timezone as _tz

        def _query_expired() -> list[tuple[str, str]]:
            SessionLocal = _get_db_session_local()
            ChatSandboxSnapshot = _get_snapshot_model()
            with SessionLocal() as db:
                rows = (
                    db.query(ChatSandboxSnapshot)
                    .filter(ChatSandboxSnapshot.expires_at < datetime.now(_tz.utc))
                    .all()
                )
                return [(r.chat_id, r.snapshot_id) for r in rows]

        expired = await asyncio.to_thread(_query_expired)
        if not expired:
            return
        logger.info("[opensandbox] snapshot_gc found %d expired", len(expired))
        # Remote DELETEs are independent of each other: run concurrently (with a cap
        # to avoid overwhelming the opensandbox server). On failure (most commonly
        # 409 SNAPSHOT::DELETE_CONFLICT: the snapshot is still referenced by a new
        # sandbox) → keep the DB row so the next GC round retries; only by not
        # deleting the DB row can we guarantee the chat PK isn't collided with and
        # overwritten by a new park, losing tracking.
        sem = asyncio.Semaphore(self._GC_DELETE_CONCURRENCY)

        async def _delete_one(snapshot_id: str) -> bool:
            async with sem:
                try:
                    await self._delete_snapshot(snapshot_id)
                    return True
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "[opensandbox] gc: delete remote snapshot=%s failed "
                        "(will retry next round): %s",
                        snapshot_id,
                        e,
                    )
                    return False

        results = await asyncio.gather(
            *(_delete_one(snap_id) for _, snap_id in expired),
        )
        for (chat_id, snapshot_id), ok in zip(expired, results):
            if not ok:
                continue
            try:
                await self._delete_snapshot_row(chat_id, snapshot_id)
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[opensandbox] gc: delete db row chat=%s failed: %s",
                    chat_id,
                    e,
                )

    async def _take_snapshot(self, sandbox_id: str) -> str:
        """POST /v1/sandboxes/{id}/snapshots → returns the snapshot_id (accepted).

        The snapshot state moves asynchronously in the background from Creating →
        Ready; the caller must ``_wait_snapshot_ready`` themselves. This only
        initiates.
        """
        url = f"{self._domain.rstrip('/')}/v1/sandboxes/{sandbox_id}/snapshots"
        headers = {"Content-Type": "application/json"}
        client = self._get_http_client()
        resp = await client.post(url, headers=headers, json={})
        if resp.status_code == 404:
            try:
                code = resp.json().get("code")
            except (ValueError, AttributeError):
                code = None
            if code == "DOCKER::SANDBOX_NOT_FOUND":
                raise SandboxNotFoundError(f"sandbox {sandbox_id} no longer exists")
        if resp.status_code >= 400:
            raise SandboxError(
                f"create snapshot failed (sandbox={sandbox_id}): "
                f"HTTP {resp.status_code} {resp.text[:300]}"
            )
        data = resp.json()
        snap_id = data.get("id") or data.get("snapshotId")
        if not snap_id:
            raise SandboxError(
                f"create snapshot returned no id (sandbox={sandbox_id}): {data!r}"
            )
        return str(snap_id)

    async def _wait_snapshot_ready(
        self, snapshot_id: str, *, timeout_s: Optional[int] = None,
    ) -> None:
        """Poll GET /v1/snapshots/{id} until state == 'Ready'.

        Measured accept → Ready ≈ 60s (docker commit). Polling step is 5s;
        ``timeout_s`` defaults to 120s
        (settings.opensandbox_snapshot_wait_timeout_s).
        """
        if timeout_s is None:
            timeout_s = settings.sandbox.opensandbox_snapshot_wait_timeout_s
        url = f"{self._domain.rstrip('/')}/v1/snapshots/{snapshot_id}"
        headers = {}
        client = self._get_http_client()
        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 404:
                        raise SandboxError(
                            f"snapshot {snapshot_id} disappeared while waiting Ready"
                        )
                    if resp.status_code >= 400:
                        raise SandboxError(
                            f"poll snapshot {snapshot_id} failed: HTTP {resp.status_code}"
                        )
                    state = (resp.json().get("status") or {}).get("state", "")
                    if state == _SNAPSHOT_STATE_READY:
                        return
                    if state in _SNAPSHOT_TERMINAL_FAILURE_STATES:
                        raise SandboxError(
                            f"snapshot {snapshot_id} reached terminal state={state!r}: {resp.text[:300]}"
                        )
                    await asyncio.sleep(5)
        except TimeoutError as exc:
            raise SandboxTimeoutError(
                f"snapshot {snapshot_id} not Ready after {timeout_s}s"
            ) from exc

    async def _delete_snapshot(self, snapshot_id: str) -> None:
        """DELETE /v1/snapshots/{id}. 404 counts as success (idempotent: may already have been deleted elsewhere)."""
        url = f"{self._domain.rstrip('/')}/v1/snapshots/{snapshot_id}"
        headers = {}
        client = self._get_http_client()
        resp = await client.delete(url, headers=headers)
        if resp.status_code == 404:
            return
        if resp.status_code >= 400:
            raise SandboxError(
                f"delete snapshot {snapshot_id} failed: HTTP {resp.status_code} {resp.text[:300]}"
            )
