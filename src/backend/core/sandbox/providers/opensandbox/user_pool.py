"""Per-user OpenSandbox warm-pool ownership and capacity."""

from __future__ import annotations

from core.sandbox.providers.opensandbox.accounting import finish_accounting

import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable, Optional

from core.sandbox.errors import SandboxError

logger = logging.getLogger(__name__)


class _JupyterUserPool:
    """User-bound jupyter idle queue (Plan F).

    Why not extend ``SandboxPool``: the general pool buckets by ``kind``; jupyter +
    myspace bind mount must bucket by ``user_id`` (user B must never get user A's
    sandbox — the bind mount would expose the other user's myspace files). Rewriting a
    lightweight structure without touching ``_pool.py`` keeps the change surface small;
    ``SandboxPool`` continues to serve light sandboxes / legacy paths.

    Plan F Q2 optimization: ``_destroy_session`` switched to ``reuse=True`` — the same
    user reuses sandboxes across chats. When ``acquire`` hits an idle sandbox it skips
    Sandbox.create (~10s) and _persistent_setup (~4s); the first bash of the same user's
    2nd+ chat drops from ~33s to <10s. Idle sandboxes carry timestamps; those exceeding
    ``idle_ttl_s`` are destroyed by the background reap to prevent leaks.

    Characteristics:
    - **No proactive warm-up**: we don't know which users will come, so no jupyter
      sandbox is created during process startup. Adopted old sandboxes enter their
      bucket by user tag; since Q2, sandboxes also enter idle when a user's chat ends.
    - **`acquire` pops on hit**: takes an (sbx, release_ts) pair and discards the ts.
    - **adopt path**: ``_adopt_existing_sandboxes`` reads the ``hugagent-user-id``
      metadata tag then calls ``adopt(user_id, sbx)`` into the matching bucket; on
      overflow (user already has max_idle) the caller destroys it, avoiding an
      in_flight leak.
    - **idle reap**: ``reap_idle(ttl_s)`` ages out and destroys expired sandboxes by
      release_ts.

    空闲队列的不变式是「里面只有无主容器」，由 ``owner_fn`` 在两个消费点把守：
    ``acquire`` 不把有主容器递给别的会话，``reap_idle`` 不把有主容器当空闲销毁。
    放在这两处而不是各个入队点，是因为入队之后容器**还会重新变成有主的**——多 worker
    下 A 把会话 park 进池时，B 的进程内会话表可能还缓存着同一个容器，下一轮请求落到 B
    就会重新绑定它。按入队时刻判断的守卫结构上盖不住这种情况，按消费时刻判断可以。
    """

    __slots__ = (
        "_idle",
        "_in_flight",
        "_factory",
        "_destroy",
        "_liveness",
        "_owner",
        "_max_idle_per_user",
        "_max_in_flight_per_user",
        "_lock",
        "_closed",
    )

    def __init__(
        self,
        *,
        factory: Any,  # async (user_id) -> sandbox
        destroy_fn: Any,  # async (sandbox) -> None
        max_idle_per_user: int = 2,
        max_in_flight_per_user: int = 8,
        liveness_fn: Any = None,  # async (sandbox) -> bool; None = no liveness probe (old behavior)
        owner_fn: Any = None,  # async (sandbox) -> session_id | None; None = 不做归属校验
    ) -> None:
        # idle now carries timestamps: deque[(sandbox, release_ts_monotonic)]
        self._idle: dict[str, deque[tuple[Any, float]]] = {}
        self._in_flight: dict[str, int] = {}
        self._factory = factory
        self._destroy = destroy_fn
        # Liveness probe before handing out an idle sandbox: dead handles already reaped
        # server-side return False → discard and take the next one.
        # See _OpenSandboxCreationMixin._probe_pooled_sandbox_alive.
        self._liveness = liveness_fn
        # 归属判定：容器当前属于哪个会话。见类文档「空闲队列的不变式」。
        self._owner = owner_fn
        self._max_idle_per_user = max_idle_per_user
        # Per-user concurrency cap, preventing a single user from pulling unlimited
        # sandboxes with N open tabs / a retry storm. Deployment capacity is the
        # shared admission ledger's job; this only bounds one user in this process.
        self._max_in_flight_per_user = max_in_flight_per_user
        self._lock = asyncio.Lock()
        self._closed = False

    async def acquire(self, user_id: str) -> Any:
        if self._closed:
            raise RuntimeError("_JupyterUserPool already closed")
        while True:
            async with self._lock:
                queue = self._idle.get(user_id)
                candidate = queue.popleft()[0] if queue else None
                count = self._in_flight.get(user_id, 0)
                if candidate is None and count >= self._max_in_flight_per_user:
                    raise SandboxError(
                        f"per-user jupyter sandbox cap reached (user={user_id}, "
                        f"in_flight={count}, cap={self._max_in_flight_per_user})"
                    )
                self._in_flight[user_id] = count + 1
            reserved = True
            try:
                if candidate is not None:
                    if await self._disown_if_owned(candidate, user_id, "acquire") is not None:
                        reserved = False
                        await finish_accounting(self._unreserve(user_id))
                        continue
                    alive = self._liveness is None or await self._liveness(candidate)
                    # Tracking can expire during the probe; never destroy or
                    # deliver a handle that another worker has since adopted.
                    if await self._disown_if_owned(candidate, user_id, "acquire") is not None:
                        reserved = False
                        await finish_accounting(self._unreserve(user_id))
                        continue
                    if not alive:
                        reserved = False
                        await finish_accounting(self._unreserve(user_id))
                        await self._safe_destroy(candidate)
                        continue
                    return candidate
                sandbox = await self._factory(user_id)
                if self._closed:
                    reserved = False
                    await finish_accounting(self._unreserve(user_id))
                    await self._safe_destroy(sandbox)
                    raise RuntimeError("_JupyterUserPool closed during sandbox creation")
                return sandbox
            except BaseException:
                if reserved:
                    await finish_accounting(self._unreserve(user_id))
                raise

    async def release(self, user_id: str, sbx: Any, *, reuse: bool, destroy: bool = True, can_destroy: Optional[Callable[[], bool]] = None) -> None:
        """Return a sandbox. ``reuse=False`` → destroy outright; ``reuse=True`` → enter idle for same-user reuse."""
        should_destroy: Optional[Any] = None
        async with self._lock:
            self._in_flight[user_id] = max(self._in_flight.get(user_id, 0) - 1, 0)
            if reuse and destroy and (can_destroy is None or can_destroy()) and not self._closed:
                q = self._idle.setdefault(user_id, deque())
                if len(q) < self._max_idle_per_user:
                    q.append((sbx, time.monotonic()))
                    logger.info(
                        "[jupyter-user-pool] release user=%s sandbox_id=%s -> idle "
                        "(idle_count=%d)",
                        user_id, getattr(sbx, "id", "?"), len(q),
                    )
                else:
                    should_destroy = sbx
            else:
                should_destroy = sbx
        if should_destroy is not None and destroy and (can_destroy is None or can_destroy()):
            await self._safe_destroy(should_destroy)

    def has_idle(self, user_id: str) -> bool:
        """Plan F Q2: non-blocking probe — does this user have an idle sandbox to take.

        ``_create_session_for`` uses this to decide between the idle-pool warm path and
        snapshot restore — an idle hit is significantly faster. No lock: an occasional
        race (seen available but snatched away at acquire) naturally degrades to fresh
        create, no correctness issue.
        """
        q = self._idle.get(user_id)
        return bool(q)

    async def adopt(self, user_id: str, sbx: Any) -> bool:
        """For the backend-restart adopt path: put a live sandbox back into this user's idle.

        ``True``: enqueued successfully; ``False``: pool closed / bucket full — the caller
        is responsible for destroying the sandbox (do not leak it into in_flight).

        入队不校验归属：有主容器由 ``acquire`` / ``reap_idle`` 的 ``owner_fn`` 兜住
        （见类文档），因为容器在队列里期间还会重新变成有主的。
        """
        if self._closed:
            return False
        async with self._lock:
            q = self._idle.setdefault(user_id, deque())
            if len(q) >= self._max_idle_per_user:
                return False
            # Treat adopt as "just released": timestamp with now, granting a full TTL
            q.append((sbx, time.monotonic()))
            return True

    async def reap_idle(self, ttl_s: float) -> int:
        """Destroy sandboxes idle for longer than ttl_s. Returns the number destroyed.

        Called by the OpenSandboxProvider.reap_idle_sessions background task with the
        shared ``SANDBOX_IDLE_TTL_S`` window (default 3600s = 1 h). Same user
        returning to a chat within the TTL → idle hit; past the TTL → the sandbox is
        destroyed and the cold path rebuilds.

        队列里的老化时间只在入队时打，之后不会被真实使用刷新——所以销毁前必须问一句
        归属：一台在池子里"闲"满一小时的容器，可能正被某个会话用得正欢。这里是数据
        丢失真正发生的地方（容器一销毁，``/workspace`` 就没了），守在这里才盖得住所有
        入队路径。
        """
        if self._closed or ttl_s <= 0:
            return 0
        now = time.monotonic()
        expired: list[tuple[str, Any]] = []
        async with self._lock:
            for uid, q in list(self._idle.items()):
                kept: deque[tuple[Any, float]] = deque()
                while q:
                    sbx, ts = q.popleft()
                    if now - ts > ttl_s:
                        expired.append((uid, sbx))
                    else:
                        kept.append((sbx, ts))
                self._idle[uid] = kept
        destroyed = 0
        for uid, sbx in expired:
            if await self._disown_if_owned(sbx, uid, "reap") is not None:
                continue
            logger.info(
                "[jupyter-user-pool] reaping idle sandbox_id=%s (idle > %ds)",
                getattr(sbx, "id", "?"),
                int(ttl_s),
            )
            await self._safe_destroy(sbx)
            destroyed += 1
        return destroyed

    async def shutdown(self) -> None:
        self._closed = True
        async with self._lock:
            buckets = list(self._idle.items())
            self._idle.clear()
        for user_id, q in buckets:
            while q:
                sbx, _ts = q.popleft()
                if await self._disown_if_owned(sbx, user_id, "shutdown") is None:
                    await self._safe_destroy(sbx)

    async def _disown_if_owned(self, sbx: Any, user_id: str, stage: str) -> Optional[str]:
        """容器已被某个会话认领就交还给它，返回会话 id；无主返回 ``None``。

        "交还"就是本池不再管它——不销毁、也不放回空闲队列（两个调用点都已经把它
        popleft 出来了）。它的生命周期归那个会话，会话结束时走
        ``_destroy_session``。判定在锁外做（要走网络 I/O），与 ``_liveness`` 同理。
        """
        if self._owner is None:
            return None
        owner = await self._owner(sbx)
        if owner is None:
            return None
        logger.info(
            "[jupyter-user-pool] %s: sandbox_id=%s user=%s is bound to session %s; "
            "dropping it from idle and leaving it to that session",
            stage,
            getattr(sbx, "id", "?"),
            user_id,
            owner,
        )
        return str(owner)

    async def _safe_destroy(self, sbx: Any) -> None:
        try:
            await self._destroy(sbx)
        except Exception as e:  # noqa: BLE001
            logger.warning("[jupyter-user-pool] destroy failed: %s", e)

    def stats(self) -> dict[str, dict[str, int]]:
        """Diagnostics: per-user idle/in_flight counts.

        Unlocked — callers accept being one beat off from concurrent acquire/release.
        But snapshot dict.keys()/values() before reading to avoid 'dictionary changed
        size during iteration'.
        """
        out: dict[str, dict[str, int]] = {}
        for uid, q in list(self._idle.items()):
            out.setdefault(uid, {"idle": 0, "in_flight": 0})["idle"] = len(q)
        for uid, cnt in list(self._in_flight.items()):
            out.setdefault(uid, {"idle": 0, "in_flight": 0})["in_flight"] = cnt
        return out

    def idle_sandboxes(self) -> list[tuple[str, Any, float]]:
        """Enumerate each user's idle jupyter sandboxes as ``(user_id, sbx, release_ts)``, for the read-only panel."""
        out: list[tuple[str, Any, float]] = []
        for uid, q in list(self._idle.items()):
            out.extend((uid, sbx, ts) for (sbx, ts) in list(q))
        return out

    async def _unreserve(self, user_id):
        async with self._lock:
            self._in_flight[user_id] = max(0, self._in_flight.get(user_id, 0) - 1)
