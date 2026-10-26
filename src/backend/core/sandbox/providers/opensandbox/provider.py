from __future__ import annotations

import asyncio
import threading
from weakref import WeakValueDictionary
from pathlib import Path
from typing import Any, Optional

import core.sandbox.sessions.registry as session_registry
from core.config.settings import settings
from core.sandbox.errors import SandboxError, SandboxFileTooLargeError
from core.sandbox.processes.completion import CompletionMixin
from core.sandbox.processes.dispatch import _on_service_loop
from core.sandbox.protocol import ProcessRequest
from core.sandbox.providers.opensandbox.admin import _OpenSandboxAdminMixin
from core.sandbox.providers.opensandbox.adoption import _OpenSandboxAdoptionMixin
from core.sandbox.providers.opensandbox.connection import _OpenSandboxConnectionMixin
from core.sandbox.providers.opensandbox.creation import _OpenSandboxCreationMixin
from core.sandbox.providers.opensandbox.execution import _OpenSandboxExecMixin
from core.sandbox.providers.opensandbox.lifecycle import _OpenSandboxLifecycleMixin
from core.sandbox.providers.opensandbox.models import SandboxSessionRecord as _Session
from core.sandbox.providers.opensandbox.pool import SandboxPool
from core.sandbox.providers.opensandbox.runtime import (
    logger,
    write_file,
)
from core.sandbox.providers.opensandbox.snapshots import _OpenSandboxSnapshotsMixin

# Snapshot persistence (see internal design docs). Deferred to
# runtime import to avoid a cross-module circular dependency at startup (models.py also
# imports core.db.engine).
from core.sandbox.providers.opensandbox.user_pool import _JupyterUserPool
from core.sandbox.workspace.streams import stream_to_file


class OpenSandboxProvider(
    CompletionMixin,
    _OpenSandboxCreationMixin,
    _OpenSandboxSnapshotsMixin,
    _OpenSandboxLifecycleMixin,
    _OpenSandboxExecMixin,
    _OpenSandboxAdminMixin,
    _OpenSandboxAdoptionMixin,
    _OpenSandboxConnectionMixin,
):
    name = "opensandbox"

    # 类属性兜底：有测试绕过 __init__ 直接造实例，亲和层读不到就会 AttributeError。
    # None 表示未锚定服务循环，装饰器退化为就地执行。
    _service_loop: Optional[asyncio.AbstractEventLoop] = None

    def __init__(self) -> None:
        # All languages execute through execd.
        try:
            from opensandbox import Sandbox
        except ImportError as e:
            raise SandboxError(
                "未安装 opensandbox SDK，请在 requirements 中加入 "
                "'opensandbox>=0.1.15' 并重建镜像"
            ) from e

        s = settings.sandbox
        # SDK and direct health/renew requests must use the same absolute endpoint.
        self._domain = s.opensandbox_domain.rstrip("/")
        if not self._domain.startswith(("http://", "https://")):
            self._domain = "http://" + self._domain
        self._api_key = s.opensandbox_api_key
        self._image = s.opensandbox_image
        self._ttl_s = s.idle_ttl_s
        self._snapshot_expiry_grace_s = (
            60 + s.opensandbox_request_timeout_s + s.opensandbox_snapshot_wait_timeout_s
        )
        self._snapshot_expiry_enabled = s.opensandbox_snapshot_enabled
        self._snapshot_capacity = max(1, s.opensandbox_pool_max_total)
        self._ready_timeout_s = s.opensandbox_ready_timeout_s
        self._request_timeout_s = s.opensandbox_request_timeout_s
        self._renew_min_interval_s = (
            60  # aligned with server-side [renew_intent].min_interval_seconds
        )

        # Cold-start speedup: skip the server's /endpoints resolution (measured at a
        # fixed ~3.3s x2) and construct the deterministic proxy endpoint locally.
        # Idempotent, with safe fallback on failure (see the method docstring).
        self._install_endpoint_fastpath()

        self._sessions: dict[str, _Session] = {}
        # 服务事件循环：所有碰 SDK / 池 / httpx 的入口都钉在它上面执行（见
        # ``_on_service_loop``）。warmup() 在 FastAPI 启动钩子里锚定它；这里先尽力
        # 取一次，覆盖不走 warmup 的调用方（测试、脚本）。取不到就保持 None，
        # 装饰器退化为直接执行，与钉循环之前的行为一致。
        try:
            self._service_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._service_loop = None
        # ⚠️ 锁必须按**事件循环**分桶。这个 provider 是进程级单例，但调用方不止一个 loop：
        # 子智能体跑在独立线程的独立事件循环里（core/llm/subagents/tool.py 的
        # asyncio.new_event_loop()）。asyncio.Lock 在**首次 await 时**绑定当时的 running loop，
        # 之后被另一个 loop 用到就抛 "is bound to a different event loop"。
        # 事故形态极隐蔽：谁先碰 provider 谁把锁绑走，另一个 loop 的沙箱保活 touch_session
        # 从此每次都抛、又被调用方 except 掉 —— 保活静默失效，沙箱空闲 600s 被回收，
        # 正在跑的批量作业连同 workspace 一起消失，而作业状态还停在 running。
        # 单锁跨 loop 本来就不可能работать（只会抛），按 loop 分桶严格优于现状。
        self._session_locks: WeakValueDictionary[tuple[int, str], asyncio.Lock] = WeakValueDictionary()
        # 注册表锁只保护上面这个 dict 的读写（临界区里没有 await），所以用线程锁即可，
        # 天然跨 loop 可用 —— 换成 asyncio.Lock 就会重新引入上面那个 bug。
        self._registry_lock = threading.Lock()
        # Shared httpx client, avoids re-doing the TLS handshake on every health/renew call
        self._http_client: Optional[Any] = None
        # Direct-execd reachability is a **process-level topology property** (not
        # per-sandbox): once judged unreachable (non-compose deployment, container name
        # not resolvable), subsequent sandboxes stop burning the probe budget one by one
        # and go straight through the server proxy. When reachable, each sandbox is
        # still probed — that probe doubles as the wait for execd boot.
        self._direct_execd_unreachable: bool = False

        # Warm pool: moves the "create sandbox" cost off the synchronous path into the
        # background. When any per-user volume is enabled, the jupyter bucket only
        # serves the fallback path where user_id is missing; normal user-bound traffic
        # goes through _jupyter_user_pool.
        self._pool = SandboxPool(
            jupyter_min_idle=0,
            jupyter_max_idle=s.opensandbox_pool_jupyter_max_idle,
            light_min_idle=s.opensandbox_pool_light_min_idle,
            light_max_idle=s.opensandbox_pool_light_max_idle,
            jupyter_factory=self._create_jupyter_sandbox,
            light_factory=self._create_light_sandbox,
            destroy_fn=self._destroy_sandbox,
            liveness_fn=self._probe_pooled_sandbox_alive,
            owner_fn=self._pooled_sandbox_owner,
        )
        # Account sessions and adopted instances share the same per-user pool.
        self._jupyter_user_pool = _JupyterUserPool(
            factory=lambda uid: self._create_jupyter_sandbox(user_id=uid),
            destroy_fn=self._destroy_sandbox,
            liveness_fn=self._probe_pooled_sandbox_alive,
            owner_fn=self._pooled_sandbox_owner,
            max_idle_per_user=max(1, s.opensandbox_pool_jupyter_max_idle),
            # One user's concurrent creations in this process; deployment capacity
            # is enforced by the shared admission ledger.
            max_in_flight_per_user=max(1, s.opensandbox_pool_max_total),
        )

        # Snapshot persistence background workers (internal design docs §5.4/§5.5).
        # The actual tasks are create_task'd in warmup() and cancelled + awaited in shutdown.
        self._snapshot_idle_task: Optional[asyncio.Task] = None
        self._snapshot_gc_task: Optional[asyncio.Task] = None
        self._snapshot_workers_shutdown: asyncio.Event = asyncio.Event()
        # Set of chat_ids currently being parked: prevents the same chat from being parked twice concurrently
        self._parking_in_flight: set[str] = set()
        # Do NOT auto-kick pool warmup in __init__: the FastAPI startup hook calls
        # ``provider.warmup()``, and that path first runs ``_adopt_existing_sandboxes``
        # (reclaiming our own still-alive sandboxes left by the previous backend
        # generation) before filling the remaining deficit. If __init__ also
        # fire-and-forgot a ``_pool.warmup()``, it would run before adoption, filling
        # idle with 4 freshly created sandboxes, so the old sandboxes found by adoption
        # could only take the overflow branch and get killed — wasting a whole round of
        # warmup cost. If there truly is a caller that skips the FastAPI startup hook,
        # the ``_kick_refill`` triggered by the first acquire covers it, with no
        # behavioral degradation.

    # ───── public API ─────────────────────────────────────────────────────

    @_on_service_loop
    async def start_process(self, req: ProcessRequest, yield_time_ms: int = 60000) -> dict:
        from core.sandbox.processes.cloud import process_service

        return await process_service(self).start(req, yield_time_ms)

    @_on_service_loop
    async def write_stdin(
        self,
        session_id: str,
        *,
        sandbox_session_id: str,
        user_id: Optional[str] = None,
        chars: str = "",
        yield_time_ms: int = 60000,
    ) -> dict:
        from core.sandbox.processes.cloud import process_service

        return await process_service(self).write(
            session_id,
            sandbox_session_id=sandbox_session_id,
            user_id=user_id,
            chars=chars,
            yield_time_ms=yield_time_ms,
        )

    async def current_sandbox_id(self, session_id: Optional[str]) -> Optional[str]:
        """Return the underlying container UUID for ``session_id`` if a session
        exists. Returns None when no session has been created yet — pure query,
        no side-effects (see ``SandboxProvider.current_sandbox_id`` contract).
        """
        if not session_id:
            return None
        return await session_registry.lookup(session_id)

    @_on_service_loop
    async def touch_session(self, session_id: str) -> bool:
        """保活入口：同样钉在服务循环上。

        keepalive 定时器和子智能体都会调它，而它碰的 ``_maybe_schedule_renew``
        会在当前循环上 ``create_task`` 一个持有 SDK 对象的 renew——跑在子智能体的
        临时循环上就会随线程退出而失效，正是保活静默失灵的来源。
        """
        return await super().touch_session(session_id)

    @_on_service_loop
    async def close_session(self, session_id: Optional[str]) -> None:
        """Explicitly destroy a persistent-session sandbox.

        For run-once-then-discard callers (sub-agents: each call creates a user-bound
        sandbox with a unique session, which must be explicitly reclaimed at the end —
        otherwise the unique session is never reused → sandbox leak). No-op if the
        session does not exist; a destroy failure only degrades to a log line, never
        raises.
        """
        processes = getattr(self, "_command_processes", None)
        if processes:
            await processes.close_owner(session_id)
        if not session_id:
            return
        lock = await self._get_session_lock(session_id)
        async with lock, session_registry.claim(session_id):
            sess = self._sessions.get(session_id)
            bound = await session_registry.lookup(session_id)
            if sess is not None and str(sess.sandbox.id) != bound:
                await self._release_session(session_id, sess, detach_only=True)
                sess = None
            if sess is None and bound:
                sess = await self._attach_session(bound, user_id=None)
                if sess is not None:
                    self._sessions[session_id] = sess
            if sess is None:
                return
            try:
                await self._destroy_session(session_id, sess, reuse_sandbox=False)
            except Exception as exc:
                logger.warning("[opensandbox] close_session failed sid=%s: %s", session_id, exc)

    @_on_service_loop
    async def put_file(
        self,
        session_id: Optional[str],
        path: str,
        content: bytes,
        user_id: Optional[str] = None,
    ) -> None:
        """Write bytes to the given path inside the sandbox.

        ``session_id`` is required — artifact in/out semantics only make sense within a
        persistent session (an ephemeral sandbox is destroyed when the session ends, so
        anything written can never be read back). ``user_id`` is passed through to
        session creation, guaranteeing that file operations that happen before any bash
        call also create a user-bound session (with the credentials volume).
        """
        if not session_id:
            raise SandboxError("sandbox put_file 需要 session_id（持久会话）；当前请求未绑定会话")
        async with self._session_use(session_id, user_id) as sess:
            try:
                await write_file(sess.sandbox, path, content)
            except Exception as e:  # noqa: BLE001
                raise SandboxError(f"write_file {path} 失败: {e}") from e

    @_on_service_loop
    async def get_file(
        self,
        session_id: Optional[str],
        path: str,
        user_id: Optional[str] = None,
    ) -> bytes:
        """Read file bytes from the sandbox. ``user_id`` is passed through to session creation (see ``put_file``)."""
        if not session_id:
            raise SandboxError("sandbox get_file 需要 session_id（持久会话）；当前请求未绑定会话")
        async with self._session_use(session_id, user_id) as sess:
            try:
                raw = await sess.sandbox.files.read_bytes(path)
            except Exception as e:  # noqa: BLE001
                raise SandboxError(f"read_bytes {path} 失败: {e}") from e
            if isinstance(raw, str):
                return raw.encode("utf-8")
            return bytes(raw)

    @_on_service_loop
    async def get_file_to_path(
        self,
        session_id: Optional[str],
        path: str,
        destination: Path,
        *,
        max_bytes: int,
        user_id: Optional[str] = None,
    ) -> int:
        """Stream an OpenSandbox file into a backend-local path."""
        if not session_id:
            raise SandboxError("sandbox get_file 需要 session_id（持久会话）；当前请求未绑定会话")
        async with self._session_use(session_id, user_id) as sess:
            try:
                info_by_path = await sess.sandbox.files.get_file_info([path])
                info = info_by_path.get(path) if isinstance(info_by_path, dict) else None
                known_size = int(getattr(info, "size", 0) or 0)
                if known_size > max_bytes:
                    raise SandboxFileTooLargeError(actual_size=known_size, max_size=max_bytes)

                chunks = await sess.sandbox.files.read_bytes_stream(path, chunk_size=1024 * 1024)
                try:
                    return await stream_to_file(chunks, destination, max_bytes=max_bytes)
                finally:
                    close = getattr(chunks, "aclose", None)
                    if close is not None:
                        await close()
            except SandboxFileTooLargeError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise SandboxError(f"stream_file {path} 失败: {exc}") from exc

    @_on_service_loop
    async def health(self) -> bool:
        """Hit the OpenSandbox server /health directly (not via the SDK)."""
        try:
            client = self._get_http_client()
            resp = await client.get(f"{self._domain.rstrip('/')}/health", timeout=3)
            return resp.status_code == 200
        except Exception:
            return False

    # ── Read-only admin interface ────────────────────────────────────────
    # OpenSandbox is the most capable backend: dual-bucket pool + per-user idle queue
    # + snapshot system. The instance list = the three in-process "managed" sources
    # (_sessions + the two idle pools, backend_managed=True) ∪ server-side "stray"
    # instances (still alive but not tracked in memory, backend_managed=False).
    # Pool figures come from the stats of the two pools.

    def bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._service_loop is not None and self._service_loop is not loop:
            raise RuntimeError("Sandbox provider already belongs to another event loop")
        self._service_loop = loop

    async def warmup(self) -> None:
        """Adopt owned instances and start pool/snapshot maintenance."""
        self.bind_loop()
        try:
            await self._adopt_existing_sandboxes()
        except Exception as exc:  # noqa: BLE001 — an adoption failure must not block warmup
            logger.warning(
                "[opensandbox] adopt existing sandboxes failed (continuing with fresh warmup): %s",
                exc,
            )
        await self._pool.warmup()
        # Snapshot background workers: gated by the master switch; idempotent (repeated calls do not double-start)
        if settings.sandbox.opensandbox_snapshot_enabled:
            if self._snapshot_idle_task is None or self._snapshot_idle_task.done():
                self._snapshot_workers_shutdown.clear()
                self._snapshot_idle_task = asyncio.create_task(self._snapshot_idle_sessions_loop())
            if self._snapshot_gc_task is None or self._snapshot_gc_task.done():
                self._snapshot_gc_task = asyncio.create_task(self._snapshot_gc_loop())

    async def stop_maintenance(self) -> None:
        # Stop the snapshot workers first: no more new park / GC triggers
        self._snapshot_workers_shutdown.set()
        for task in (self._snapshot_idle_task, self._snapshot_gc_task):
            if task is None or task.done():
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._snapshot_idle_task = None
        self._snapshot_gc_task = None

    async def shutdown(self) -> None:
        """Called on process exit: gracefully shut down all persistent sessions + idle sandboxes in the pools + snapshot workers."""
        processes = getattr(self, "_command_processes", None)
        if processes:
            await processes.close_all()
        await self.stop_maintenance()

        for sid, sess in list(self._sessions.items()):
            try:
                await self._release_session(sid, sess, detach_only=True)
            except Exception as e:  # noqa: BLE001
                logger.warning("[opensandbox] cleanup session=%s failed: %s", sid, e)
        self._sessions.clear()
        await self._pool.shutdown()
        await self._jupyter_user_pool.shutdown()
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            finally:
                self._http_client = None

    # ───── connection / session management ────────────────────────────────

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        """取本 session 在**当前事件循环**里的锁（见 __init__ 处的长注释）。"""
        key = (id(asyncio.get_running_loop()), session_id)
        with self._registry_lock:
            lock = self._session_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[key] = lock
            return lock

    def _session_expiry_s(self) -> int:
        """Bound hard expiry after the local snapshot backlog and renewal skew."""
        if not getattr(self, "_snapshot_expiry_enabled", False):
            return self._ttl_s
        capacity = max(self._snapshot_capacity, len(self._sessions) + 1)
        # One scan interval per queued session is conservative relative to the
        # three-at-a-time parker. Recompute on creation and activity renewal.
        return (self._ttl_s + capacity * self._SNAPSHOT_IDLE_LOOP_INTERVAL_S
                + self._snapshot_expiry_grace_s)
