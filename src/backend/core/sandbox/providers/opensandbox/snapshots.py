"""OpenSandbox session/snapshot/creation methods (mixin)."""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

import core.sandbox.sessions.registry as session_registry
from core.config.settings import settings
from core.sandbox.errors import SandboxNotFoundError, SandboxError
from core.sandbox.providers.opensandbox.models import SandboxSessionRecord as _Session

# Snapshot persistence (see internal design docs). Imports are
# deferred to runtime to avoid cross-module circular dependencies at startup
# (models.py also imports core.db.engine).
from core.sandbox.providers.opensandbox.runtime import logger
import core.sandbox.providers.opensandbox.snapshot_store as snapshot_store
from core.sandbox.providers.opensandbox.snapshot_store import (
    _SNAPSHOT_STATE_MISSING,
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

    # Snapshots the controller may be creating at once. It runs two snapshot
    # workers; a third request only queues behind them on the same disk and eats
    # into its own controller deadline.
    _SNAPSHOT_CONCURRENCY = 2

    _SNAPSHOT_IDLE_LOOP_INTERVAL_S = 60

    # Backoff after a failed snapshot: 5 min, doubling, capped at one hour. The
    # session stays bound and renewed meanwhile, so the workspace is not lost.
    _SNAPSHOT_RETRY_BASE_S = 300

    _SNAPSHOT_RETRY_MAX_S = 3600

    # A job the controller has not settled long after its own 180 s deadline
    # (controller restarted, record lost) or a Ready one no scan could commit
    # would hold a concurrency slot forever. Past this age it is failed and its
    # snapshot deleted, freeing the slot.
    _SNAPSHOT_SETTLE_DEADLINE_S = 1800

    # Snapshot start and last use are stamped by different processes, possibly
    # on different hosts: use this close before the start counts as after it.
    _SNAPSHOT_CLOCK_SKEW_S = 30

    _SNAPSHOT_GC_LOOP_INTERVAL_S = 3600

    _GC_DELETE_CONCURRENCY = 5

    _DELETION_BATCH_LIMIT = 20

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
        """Every scan: settle snapshot jobs, then snapshot more idle sessions.

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
        """One scan of the idle-reclaim state machine.

        A controller snapshot is an asynchronous job whose outcome only the
        controller knows. Nothing here waits for it: each scan reads the state of
        every recorded job and acts on what the controller reports, then starts
        new snapshots while fewer than ``_SNAPSHOT_CONCURRENCY`` are in flight.
        Jobs are rows, so a restart or a new maintenance leader picks them up.
        """
        await self._discover_idle_sessions()
        entries = list(self._sessions.items())
        # Every idle session, including one whose snapshot is still being
        # created, is renewed so the container outlives the scan backlog.
        await self._protect_queued_snapshots(entries)
        jobs = await asyncio.to_thread(snapshot_store.load_jobs)
        # Children belong to the maintenance task: losing leadership cancels
        # and joins them before releasing ownership.
        async with asyncio.TaskGroup() as group:
            for job in jobs.values():
                group.create_task(self._settle_snapshot_job(job))
        jobs = await asyncio.to_thread(snapshot_store.load_jobs)
        in_flight = sum(job.state == snapshot_store.JOB_CREATING for job in jobs.values())
        # Settling released the parked sessions; pick from what is still bound.
        await self._start_idle_snapshots(
            list(self._sessions.items()), jobs, self._SNAPSHOT_CONCURRENCY - in_flight,
        )
        await self._drain_snapshot_deletions()

    # ───── settling recorded jobs ─────────────────────────────────────────

    async def _settle_snapshot_job(self, job: snapshot_store.SnapshotJob) -> None:
        try:
            if job.state == snapshot_store.JOB_CREATING:
                if self._snapshot_job_overdue(job):
                    await self._fail_snapshot_job(
                        job,
                        f"not settled within {self._SNAPSHOT_SETTLE_DEADLINE_S}s",
                        remote_left=True,
                    )
                    return
                state = await self._snapshot_state(job.snapshot_id)
                if state == _SNAPSHOT_STATE_READY:
                    await self._commit_snapshot_job(job)
                elif state == _SNAPSHOT_STATE_MISSING:
                    await self._fail_snapshot_job(
                        job, "snapshot disappeared on the controller", remote_left=False,
                    )
                elif state in _SNAPSHOT_TERMINAL_FAILURE_STATES:
                    await self._fail_snapshot_job(
                        job, f"controller reported {state}", remote_left=True,
                    )
                # Anything else is the controller still working: the next scan looks again.
            elif await session_registry.lookup(job.chat_id) != job.sandbox_id:
                # The backed-off session is gone; there is nothing left to snapshot.
                await asyncio.to_thread(snapshot_store.forget_job, job.chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — the row stays; the next scan retries
            logger.warning(
                "[opensandbox] settle snapshot job chat=%s snapshot=%s failed: %s",
                job.chat_id, job.snapshot_id, e,
            )

    async def _commit_snapshot_job(self, job: snapshot_store.SnapshotJob) -> None:
        """A Ready snapshot becomes the restore point and the container is released.

        Only when the chat is still on the snapshotted sandbox and has not been
        used since the snapshot started; otherwise the snapshot may miss work
        and is discarded. The next idle scan takes a fresh one.
        """
        chat_id = job.chat_id
        sess = self._sessions.get(chat_id)
        if sess is None or str(sess.sandbox.id) != job.sandbox_id:
            if await self._snapshot_job_is_current(job):
                return  # discovery has not attached the session yet
            await self._discard_snapshot_job(job)
            return
        lock = await self._get_session_lock(chat_id)
        if lock.locked():
            return  # someone is using it; the next scan decides
        async with lock, session_registry.claim(chat_id):
            if self._sessions.get(chat_id) is not sess:
                return
            if not await self._snapshot_job_is_current(job):
                await self._discard_snapshot_job(job)
                return
            old_snapshot_id = await asyncio.to_thread(
                snapshot_store.promote_job,
                chat_id,
                job.snapshot_id,
                expires_at=self._snapshot_expires_at(),
            )
            # The snapshot carries the workspace now: hand the wiped container
            # back to the user's idle pool for the warm path.
            await self._destroy_session(chat_id, sess, reuse_sandbox=True)
        logger.info(
            "[opensandbox] parked chat=%s snapshot=%s (replaced_old=%s)",
            chat_id, job.snapshot_id, old_snapshot_id or "none",
        )

    async def _snapshot_job_is_current(self, job: snapshot_store.SnapshotJob) -> bool:
        started = job.started_at.timestamp() - self._SNAPSHOT_CLOCK_SKEW_S
        return (
            await session_registry.lookup(job.chat_id) == job.sandbox_id
            and not await session_registry.used_since(job.chat_id, started)
        )

    def _snapshot_job_overdue(self, job: snapshot_store.SnapshotJob) -> bool:
        from datetime import datetime, timezone

        age = (datetime.now(timezone.utc) - job.started_at).total_seconds()
        return age > self._SNAPSHOT_SETTLE_DEADLINE_S

    def _snapshot_expires_at(self):
        from datetime import datetime, timedelta, timezone

        return datetime.now(timezone.utc) + timedelta(
            days=settings.sandbox.opensandbox_snapshot_retention_days
        )

    async def _fail_snapshot_job(
        self, job: snapshot_store.SnapshotJob, reason: str, *, remote_left: bool,
    ) -> None:
        await asyncio.to_thread(
            snapshot_store.record_failure,
            job.chat_id,
            sandbox_id=job.sandbox_id,
            attempt_snapshot_id=job.snapshot_id,
            remote_left=remote_left,
            started_at=job.started_at,
            error=reason,
            retry_after=self._snapshot_retry_after,
        )
        logger.warning(
            "[opensandbox] snapshot chat=%s sandbox=%s snapshot=%s failed: %s",
            job.chat_id, job.sandbox_id, job.snapshot_id, reason,
        )

    def _snapshot_retry_after(self, failures: int):
        from datetime import datetime, timedelta, timezone

        delay = min(
            self._SNAPSHOT_RETRY_BASE_S * 2 ** max(0, failures - 1),
            self._SNAPSHOT_RETRY_MAX_S,
        )
        return datetime.now(timezone.utc) + timedelta(seconds=delay)

    async def _discard_snapshot_job(self, job: snapshot_store.SnapshotJob) -> None:
        await asyncio.to_thread(
            snapshot_store.discard_job, job.chat_id, job.snapshot_id,
            "chat changed or moved since the snapshot started",
        )
        logger.info(
            "[opensandbox] discarded snapshot=%s chat=%s: chat changed since it started",
            job.snapshot_id, job.chat_id,
        )

    async def _drain_snapshot_deletions(self) -> None:
        """Delete queued snapshots; a refused delete stays queued for the next scan.

        Least-tried first, so one snapshot the controller keeps refusing cannot
        starve the rest of the queue.
        """
        pending = await asyncio.to_thread(
            snapshot_store.load_deletions, self._DELETION_BATCH_LIMIT,
        )
        sem = asyncio.Semaphore(self._GC_DELETE_CONCURRENCY)

        async def delete(snapshot_id: str) -> None:
            async with sem:
                try:
                    await self._delete_snapshot(snapshot_id)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    attempts = await asyncio.to_thread(
                        snapshot_store.deletion_failed, snapshot_id, str(e),
                    )
                    log = logger.error if attempts and attempts % 60 == 0 else logger.warning
                    log("[opensandbox] delete snapshot=%s failed (attempt %d): %s",
                        snapshot_id, attempts, e)
                    return
                await asyncio.to_thread(snapshot_store.deletion_done, snapshot_id)

        await asyncio.gather(*(delete(snapshot_id) for snapshot_id in pending))

    # ───── starting new snapshots ─────────────────────────────────────────

    async def _start_idle_snapshots(
        self,
        entries: list[tuple[str, _Session]],
        jobs: dict[str, snapshot_store.SnapshotJob],
        slots: int,
    ) -> None:
        """Start snapshots for idle sessions while the controller has room.

        The scan resumes after the last chat it picked, so a backlog is served
        in turn rather than the same chats first every time.
        """
        if slots <= 0 or not entries:
            return
        from datetime import datetime, timezone

        threshold = settings.sandbox.idle_ttl_s
        now = time.monotonic()
        wall_now = datetime.now(timezone.utc)
        candidates: list[tuple[str, _Session]] = []
        offset = getattr(self, "_snapshot_scan_offset", 0) % len(entries)
        entries = entries[offset:] + entries[:offset]
        for index, (chat_id, sess) in enumerate(entries):
            self._snapshot_scan_offset = offset + index + 1
            job = jobs.get(chat_id)
            if job is not None and not job.may_retry(wall_now):
                continue
            if now - sess.last_active_ts < threshold:
                continue
            # 本进程闲着，不代表这个会话闲着：多 worker 下相邻两轮可能都由别人服务。
            # 回收是「快照 + 销毁容器」，按局部视角判断会把别人正在用的容器收掉。
            if await session_registry.in_use_elsewhere(chat_id, threshold):
                continue
            candidates.append((chat_id, sess))
            if len(candidates) >= slots:
                break
        async with asyncio.TaskGroup() as group:
            for chat_id, sess in candidates:
                group.create_task(self._start_snapshot(chat_id, sess))

    async def _start_snapshot(self, chat_id: str, sess: _Session) -> None:
        """Ask the controller for a snapshot and record the job; never wait for it.

        A confirmed missing sandbox retires its session and reservation. A
        rejected request backs the chat off like a failed snapshot.
        """
        from datetime import datetime, timezone

        sandbox_id = str(sess.sandbox.id)
        try:
            # Idempotency guard inside the lock: failing to take the lock = someone else is using it, skip this attempt
            lock = await self._get_session_lock(chat_id)
            if lock.locked():
                logger.info("[opensandbox] snapshot chat=%s skipped: session lock held", chat_id)
                return
            async with lock, session_registry.claim(chat_id):
                # double-check: the session may have been replaced/destroyed in the instant of taking the lock
                if self._sessions.get(chat_id) is not sess:
                    return
                if await session_registry.lookup(chat_id) != sandbox_id:
                    await self._release_session(chat_id, sess, detach_only=True)
                    return
                # 拿到跨进程认领后再确认一次全局空闲：等锁期间别的 worker 可能刚服务过它。
                if await session_registry.in_use_elsewhere(
                    chat_id, settings.sandbox.idle_ttl_s
                ):
                    logger.info(
                        "[opensandbox] snapshot chat=%s skipped: still in use elsewhere", chat_id,
                    )
                    return
                # The scan's view of the jobs may predate a leadership handover:
                # only the row read under the claim says this chat is free.
                previous = await asyncio.to_thread(snapshot_store.get_job, chat_id)
                if previous is not None and not previous.may_retry(datetime.now(timezone.utc)):
                    return
                failures = previous.failures if previous is not None else 0
                try:
                    await self._renew(sess)
                    await session_registry.renew_binding(chat_id, sandbox_id)
                except Exception as exc:
                    logger.warning("[opensandbox] pre-snapshot renewal failed sandbox=%s: %s",
                                   sandbox_id, exc)
                # Before the request: use after this instant invalidates the snapshot.
                started_at = datetime.now(timezone.utc)
                try:
                    # Shielded: once the controller accepts, the job must be
                    # recorded even if leadership is lost meanwhile.
                    snapshot_id = await asyncio.shield(self._request_snapshot(
                        chat_id, sandbox_id, started_at=started_at, failures=failures,
                    ))
                except SandboxNotFoundError:
                    # Still under the session lock and shared claim. Do not
                    # reuse a missing container or charge its reservation forever.
                    sess.stale_marked = True
                    await self._destroy_session(chat_id, sess)
                    await asyncio.to_thread(snapshot_store.forget_job, chat_id)
                    logger.warning(
                        "[opensandbox] retired missing sandbox=%s session=%s",
                        sandbox_id, chat_id,
                    )
                    return
                except SandboxError as exc:
                    await asyncio.to_thread(
                        snapshot_store.record_failure,
                        chat_id,
                        sandbox_id=sandbox_id,
                        attempt_snapshot_id=None,
                        remote_left=False,
                        started_at=started_at,
                        error=str(exc),
                        retry_after=self._snapshot_retry_after,
                    )
                    logger.warning("[opensandbox] snapshot chat=%s rejected: %s", chat_id, exc)
                    return
                logger.info(
                    "[opensandbox] snapshot chat=%s sandbox=%s started snapshot=%s",
                    chat_id, sandbox_id, snapshot_id,
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[opensandbox] snapshot chat=%s sandbox=%s failed to start: %s",
                chat_id, sandbox_id, e,
            )

    async def _request_snapshot(
        self, chat_id: str, sandbox_id: str, *, started_at, failures: int,
    ) -> str:
        snapshot_id = await self._take_snapshot(sandbox_id)
        try:
            await asyncio.to_thread(
                snapshot_store.record_creating,
                chat_id,
                sandbox_id=sandbox_id,
                snapshot_id=snapshot_id,
                started_at=started_at,
                failures=failures,
            )
        except Exception:
            logger.error(
                "[opensandbox] snapshot=%s chat=%s accepted but not recorded; "
                "delete it on the controller", snapshot_id, chat_id,
            )
            raise
        return snapshot_id

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

        The controller moves it from Creating to Ready or Failed on its own
        schedule; the caller records a job and later scans read the outcome
        with ``_snapshot_state``.
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

    async def _snapshot_state(self, snapshot_id: str) -> str:
        """One read of GET /v1/snapshots/{id}: ``status.state``, or Missing on 404.

        Other HTTP errors raise; the job is left as it is for the next scan.
        """
        url = f"{self._domain.rstrip('/')}/v1/snapshots/{snapshot_id}"
        resp = await self._get_http_client().get(url)
        if resp.status_code == 404:
            return _SNAPSHOT_STATE_MISSING
        if resp.status_code >= 400:
            raise SandboxError(f"poll snapshot {snapshot_id} failed: HTTP {resp.status_code}")
        return (resp.json().get("status") or {}).get("state", "")

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
