"""SandboxPool — OpenSandbox sandbox pre-warm pool.

Dual-bucket design:
- ``jupyter`` bucket: for persistent sessions; each sandbox includes a Jupyter Server,
  slow to start (~10s) but supports variable persistence across calls. After ``acquire``
  it is bound to a chat_id and **never returned to the bucket** — governed by the session
  lifecycle; on destruction a replacement is created asynchronously in the background.
- ``light`` bucket: for ephemeral one-shot execution; each sandbox only runs execd
  (no Jupyter), fast to start (~3s). After ``acquire`` it is used once and destroyed;
  a replacement is created asynchronously in the background.

Proactive pre-warm: right after ``provider.__init__`` completes, ``warmup()`` is called
fire-and-forget, so ``min_idle`` sandboxes are ready within N seconds of process startup.

Concurrency protection:
- Per-bucket ``deque`` + pop on ``acquire`` (O(1), lock-free, atomic);
- ``in_flight`` tracks sandboxes being created / in use for diagnostics;
  deployment capacity is the shared admission ledger's job (``admission.py``),
  not a per-process count;
- ``_dispatch_lock`` only protects the "should we create a replacement" decision;
  it does not block the actual creation.

Not persisted: the pool lives only for the process lifetime; after a process restart the
pool is empty and proactive pre-warm refills it.
"""

from __future__ import annotations

from core.sandbox.errors import SandboxCapacityExhausted
from core.sandbox.providers.opensandbox.accounting import finish_accounting
from core.sandbox.providers.opensandbox.admission import background

import asyncio
import logging
from collections import deque
from typing import Any, Awaitable, Callable, Literal

logger = logging.getLogger(__name__)

PoolKind = Literal["jupyter", "light"]


class _Bucket:
    __slots__ = ("idle", "in_flight", "min_idle", "max_idle", "factory")

    def __init__(
        self,
        min_idle: int,
        max_idle: int,
        factory: Callable[[], Awaitable[Any]],
    ):
        self.idle: deque[Any] = deque()
        self.in_flight: int = 0  # count of being-created + dispatched-but-not-yet-destroyed
        self.min_idle = min_idle
        self.max_idle = max_idle
        self.factory = factory


class SandboxPool:
    def __init__(
        self,
        *,
        jupyter_min_idle: int = 1,
        jupyter_max_idle: int = 3,
        light_min_idle: int = 2,
        light_max_idle: int = 5,
        jupyter_factory: Callable[[], Awaitable[Any]],
        light_factory: Callable[[], Awaitable[Any]],
        destroy_fn: Callable[[Any], Awaitable[None]],
        liveness_fn: Callable[[Any], Awaitable[bool]] | None = None,
        owner_fn: Callable[[Any], Awaitable[str | None]] | None = None,
    ):
        """
        Args:
            *_min_idle: minimum idle count each bucket maintains; warmup fills to this number at process startup
            *_max_idle: maximum idle count per bucket; sandboxes returned beyond it are destroyed outright
            *_factory: async factory creating a sandbox of the corresponding kind
            destroy_fn: async sandbox destruction (kill + close)
            liveness_fn: optional — liveness probe ``sbx -> bool`` run before handing out an
                idle sandbox. ``False`` means the container behind that idle handle has been
                reclaimed server-side (out-of-band reap / TTL expiry); ``acquire`` will drop it
                and take the next one / fall back to creating fresh via factory.
                ``None`` restores the old behavior (no probing).
        """
        self._buckets: dict[PoolKind, _Bucket] = {
            "jupyter": _Bucket(jupyter_min_idle, jupyter_max_idle, jupyter_factory),
            "light": _Bucket(light_min_idle, light_max_idle, light_factory),
        }
        self._destroy = destroy_fn
        self._liveness = liveness_fn
        self._owner = owner_fn
        self._dispatch_lock = asyncio.Lock()
        # One refill lock per bucket: keeps _refill coroutines of the same kind serialized,
        # preventing concurrent _kick_refill calls from entering the deficit check simultaneously and over-creating
        self._refill_locks: dict[PoolKind, asyncio.Lock] = {
            "jupyter": asyncio.Lock(),
            "light": asyncio.Lock(),
        }
        self._closed = False
        self._refill_tasks: set[asyncio.Task] = set()

    # ───── public API ─────────────────────────────────────────────────────

    async def acquire(self, kind: PoolKind) -> Any:
        if self._closed:
            raise RuntimeError("SandboxPool already closed")
        bucket = self._buckets[kind]
        while True:
            async with self._dispatch_lock:
                sandbox = bucket.idle.popleft() if bucket.idle else None
                bucket.in_flight += 1
            reserved = True
            try:
                if sandbox is not None:
                    if self._owner is not None and await self._owner(sandbox):
                        reserved = False
                        await finish_accounting(self._unreserve(bucket))
                        continue
                    alive = self._liveness is None or await self._liveness(sandbox)
                    # A slow probe can outlive the worker tracking lease. Reclaim
                    # atomically before either delivery or destruction.
                    if self._owner is not None and await self._owner(sandbox):
                        reserved = False
                        await finish_accounting(self._unreserve(bucket))
                        continue
                    if not alive:
                        reserved = False
                        await finish_accounting(self._unreserve(bucket))
                        await self._safe_destroy(sandbox)
                        continue
                else:
                    sandbox = await bucket.factory()
                self._kick_refill(kind)
                return sandbox
            except BaseException:
                if reserved:
                    await finish_accounting(self._unreserve(bucket))
                raise

    async def release(self, kind: PoolKind, sbx: Any, *, reuse: bool, destroy: bool = True, can_destroy: Callable[[], bool] | None = None) -> None:
        """Return a sandbox.

        ``reuse=False`` (light-bucket use-once-then-destroy scenario): destroy outright, refill one in the background;
        ``reuse=True`` (currently unused): put back into the idle bucket for the next acquire; destroy if over max_idle.
        """
        bucket = self._buckets[kind]
        async with self._dispatch_lock:
            bucket.in_flight -= 1
            if reuse and destroy and (can_destroy is None or can_destroy()) and len(bucket.idle) < bucket.max_idle:
                bucket.idle.append(sbx)
                return
        # destroy happens asynchronously outside the lock
        if destroy and (can_destroy is None or can_destroy()):
            await self._safe_destroy(sbx)
        self._kick_refill(kind)

    async def warmup(self) -> None:
        """Fill every bucket up to min_idle. Safe to call repeatedly."""
        for kind in self._buckets:
            self._kick_refill(kind)

    async def sweep_and_refill(self) -> int:
        """Background periodic maintenance: probe idle handles, evict server-reclaimed dead ones, then refill to min_idle.

        Pre-warmed sandboxes are **never renewed** — they get reclaimed when the
        server-side TTL (``settings.sandbox.idle_ttl_s``) hits.
        The problem: ``_kick_refill`` only fires on ``acquire``/``release``, so during
        zero-traffic periods nothing triggers it → after 30min the whole pool is drained
        into a heap of dead handles, and every user's first sandbox creation afterwards
        pays the ~10s fresh-build cost.

        This method is called by an outer scheduled task (``api/app.py``, every 120s) to
        do "evict each dead one as found, refill right after": probe out the idle handles
        already reclaimed server-side and destroy them, then ``_kick_refill`` back up to
        min_idle. Thus the warm pool keeps min_idle sandboxes that are **definitely alive
        right now** even with no traffic, and ``acquire`` reliably hits a warm sandbox.
        **No renewal, only fresh replacements** — exactly the "let it expire naturally,
        restock in the background ahead of time" approach.

        Orthogonal to the liveness probe in ``acquire``: ``acquire`` is the last line of
        defense on the request path (in case one dies right between two sweeps and gets
        picked up), while this method is proactive keep-alive off the request path.

        Returns:
            Number of dead handles evicted this round. Without ``liveness_fn`` this
            degenerates to pure refill (``_kick_refill`` only).
        """
        if self._closed:
            return 0
        evicted = 0
        for kind, bucket in self._buckets.items():
            if self._liveness is not None:
                # Take a snapshot (probing is network I/O; don't hold dispatch_lock). Idle
                # handles are independent of each other → probe concurrently; serial probing
                # would just waste time (probe all min_idle at once). Probe exceptions are
                # treated as "alive" — no false eviction; leave it to acquire / the next round.
                # Dead handles are removed back inside the lock (skip those already taken by a
                # concurrent acquire — acquire's own probe handles them); only destroy the ones
                # we actually removed.
                async with self._dispatch_lock:
                    candidates = list(bucket.idle)
                if candidates:
                    async def probe(sbx):
                        if self._owner is not None and await self._owner(sbx):
                            return "foreign"
                        return await self._liveness(sbx)
                    results = await asyncio.gather(
                        *(probe(sbx) for sbx in candidates), return_exceptions=True,
                    )
                    dead = [s for s, r in zip(candidates, results) if r is False or r == "foreign"]
                    foreign = {id(s) for s, r in zip(candidates, results) if r == "foreign"}
                    if dead:
                        async with self._dispatch_lock:
                            removed = []
                            for sbx in dead:
                                try:
                                    bucket.idle.remove(sbx)
                                    removed.append(sbx)
                                except ValueError:
                                    pass
                        for sbx in removed:
                            logger.info(
                                "[sandbox-pool] sweep: evicting dead idle sandbox kind=%s "
                                "id=%s (server-side reaped); refilling",
                                kind, getattr(sbx, "id", "?"),
                            )
                            if (id(sbx) not in foreign
                                    and (self._owner is None or not await self._owner(sbx))):
                                await self._safe_destroy(sbx)
                        evicted += len(removed)
            # Whether anything was evicted or not, refill to min_idle (built in the background, off the user request path)
            self._kick_refill(kind)
        return evicted

    async def adopt_idle(self, kind: PoolKind, sbx: Any) -> bool:
        """Push an externally pre-existing sandbox into the ``kind`` bucket's idle deque.

        Purpose: at process startup, ``provider._adopt_existing_sandboxes`` discovers our
        own sandboxes on the OpenSandbox server that "the previous backend generation left
        behind and are still alive", ``Sandbox.connect``s to them and pushes them back into
        the pool through this entry point. Equivalent to the inverse of
        ``acquire+release(reuse=True)`` but bypassing ``factory()`` — saving the cost of
        rebuilding the container and waiting for execd to become ready.

        Returns:
            ``True``: successfully added to the idle queue;
            ``False``: pool already at ``max_idle`` or closed — the caller is responsible
            for destroying this sandbox (do not leak the in_flight count).
        """
        if self._closed:
            return False
        bucket = self._buckets[kind]
        async with self._dispatch_lock:
            if len(bucket.idle) >= bucket.max_idle:
                return False
            bucket.idle.append(sbx)
            return True

    async def shutdown(self) -> None:
        """Destroy all sandboxes and stop background refills."""
        self._closed = True
        # Cancel unfinished refill tasks
        for t in list(self._refill_tasks):
            t.cancel()
        # Wait for the cancellations to complete
        for t in list(self._refill_tasks):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # Destroy all idle sandboxes (in_flight ones are not our responsibility)
        for bucket in self._buckets.values():
            while bucket.idle:
                sbx = bucket.idle.popleft()
                if self._owner is None or not await self._owner(sbx):
                    await self._safe_destroy(sbx)

    def stats(self) -> dict[str, dict[str, int]]:
        return {
            kind: {
                "idle": len(b.idle),
                "in_flight": b.in_flight,
                "min_idle": b.min_idle,
                "max_idle": b.max_idle,
            }
            for kind, b in self._buckets.items()
        }

    def idle_sandboxes(self) -> list[tuple[str, Any]]:
        """Enumerate the idle pre-warmed sandboxes ``(kind, sbx)`` in each bucket, for the read-only admin panel's instance listing.

        No locking; a snapshot read suffices (tolerates being one beat off from concurrent
        acquire/release). Note these are idle references from the **backend's in-memory
        view** — if the server has reclaimed them per TTL and the pool did not renew, the
        list may contain dead references (see the state annotation in
        OpenSandboxProvider.admin_list_sandboxes).
        """
        out: list[tuple[str, Any]] = []
        for kind, b in self._buckets.items():
            out.extend((kind, sbx) for sbx in list(b.idle))
        return out

    # ───── internals ──────────────────────────────────────────────────────

    def _kick_refill(self, kind: PoolKind) -> None:
        """Asynchronously trigger a bucket refill: does not block the acquire/release path."""
        if self._closed:
            return
        # If a refill is already running, don't queue a new task — saves create_task scheduling overhead
        if self._refill_locks[kind].locked():
            return
        task = asyncio.create_task(self._refill(kind))
        self._refill_tasks.add(task)
        task.add_done_callback(self._refill_tasks.discard)

    async def _refill(self, kind: PoolKind) -> None:
        # Serialize per kind: prevents concurrent _refills from all seeing the same deficit and creating duplicates
        if self._refill_locks[kind].locked():
            return  # a refill is already running; let it finish
        async with self._refill_locks[kind]:
            await self._refill_inner(kind)

    async def _refill_inner(self, kind: PoolKind) -> None:
        bucket = self._buckets[kind]
        while not self._closed:
            async with self._dispatch_lock:
                deficit = bucket.min_idle - len(bucket.idle)
                if deficit <= 0:
                    return
                bucket.in_flight += 1
            try:
                # Pre-warm never waits for capacity nor takes the users' headroom.
                with background():
                    sbx = await bucket.factory()
            except asyncio.CancelledError:
                async with self._dispatch_lock:
                    bucket.in_flight -= 1
                raise
            except SandboxCapacityExhausted:
                # Capacity is in use by real work; the next sweep tries again.
                async with self._dispatch_lock:
                    bucket.in_flight -= 1
                return
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "[sandbox-pool] refill kind=%s factory failed: %s",
                    kind,
                    e,
                )
                async with self._dispatch_lock:
                    bucket.in_flight -= 1
                # Brief delay between attempts to avoid hammering the docker daemon
                await asyncio.sleep(2)
                continue
            async with self._dispatch_lock:
                bucket.in_flight -= 1
                if self._closed or len(bucket.idle) >= bucket.max_idle:
                    # Shutting down or full — destroy instead
                    pass_to_destroy = sbx
                else:
                    bucket.idle.append(sbx)
                    pass_to_destroy = None
            if pass_to_destroy is not None:
                await self._safe_destroy(pass_to_destroy)
                return

    async def _safe_destroy(self, sbx: Any) -> None:
        # A destroy failure = a sandbox container leaked in the docker daemon; must not be buried at debug level
        try:
            await self._destroy(sbx)
        except Exception as e:  # noqa: BLE001
            logger.warning("[sandbox-pool] destroy failed: %s", e)

    async def _unreserve(self, bucket):
        async with self._dispatch_lock:
            bucket.in_flight -= 1
