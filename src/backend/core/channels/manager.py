"""Long-connection lifecycle manager (backend keeps one resident WS per bot).

On startup, scans all enabled long_conn connections and establishes them; at runtime,
binds/unbinds individual connections. Disconnects are reconnected internally by the SDK;
thread-level exceptions (e.g. auth failure) go through backoff retry and write back
status/last_error.

Long-connection SDK callbacks fire on a **separate thread**; the normalized ``InboundMsg``
is delivered via ``run_coroutine_threadsafe`` back to ``handle_inbound`` on the main event
loop (DB/orchestration all happen on the main-loop side).

Webhook-mode connections are not managed here (no resident connection; triggered by the
HTTP entry point). See internal design docs.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_MAX_BACKOFF = 60.0


class _Worker:
    def __init__(self, channel_id: str):
        self.channel_id = channel_id
        self.thread: Optional[threading.Thread] = None
        self.client = None
        self.stop_flag = threading.Event()
        # Event loop used for the current connection round. On stop, an external thread
        # must call_soon_threadsafe(loop.stop) to wake the worker blocked in client.start()
        # (lark ws's start blocks forever; setting stop_flag alone cannot interrupt it →
        # after the old connection is deleted, the worker becomes a zombie and keeps
        # stealing messages of the same app).
        self.loop: Optional[asyncio.AbstractEventLoop] = None


class ChannelManager:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._workers: Dict[str, _Worker] = {}
        self._lock = threading.Lock()

    # ── Startup / shutdown ──────────────────────────────────────────────
    async def start_all(self) -> None:
        """Call on the main event loop: capture the loop, scan enabled long connections and start each."""
        self._loop = asyncio.get_running_loop()
        from core.db.engine import SessionLocal
        from core.db.repository.channel import ChannelConnectionRepository

        with SessionLocal() as db:
            rows = ChannelConnectionRepository(db).list_active()
            specs = [
                (r.channel_id, r.channel_type, r.transport)
                for r in rows
                if r.transport == "long_conn"
            ]
        for channel_id, channel_type, _ in specs:
            self.start_connection(channel_id, channel_type)
        if specs:
            logger.info("[channels] 已拉起 %d 条长连接", len(specs))

    def shutdown(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            w.stop_flag.set()
            self._safe_disconnect(w)

    # ── Bind / unbind a single connection (at runtime, no restart needed) ──
    def start_connection(self, channel_id: str, channel_type: str) -> None:
        if self._loop is None:
            logger.warning("[channels] manager 未初始化，跳过 %s", channel_id)
            return
        with self._lock:
            if channel_id in self._workers:
                return
            worker = _Worker(channel_id)
            self._workers[channel_id] = worker
        t = threading.Thread(
            target=self._run_worker,
            args=(worker, channel_type),
            name=f"channel-ws:{channel_id[:8]}",
            daemon=True,
        )
        worker.thread = t
        t.start()

    def stop_connection(self, channel_id: str) -> None:
        with self._lock:
            worker = self._workers.pop(channel_id, None)
        if worker is None:
            return
        worker.stop_flag.set()
        self._safe_disconnect(worker)

    def restart_connection(self, channel_id: str, channel_type: str) -> None:
        self.stop_connection(channel_id)
        self.start_connection(channel_id, channel_type)

    # ── Thread body ─────────────────────────────────────────────────────
    def _run_worker(self, worker: _Worker, channel_type: str) -> None:
        from core.channels.registry import get_adapter

        adapter = get_adapter(channel_type)
        backoff = 1.0
        while not worker.stop_flag.is_set():
            # Use a **brand-new event loop** for every connection attempt. Some SDKs'
            # coroutines (e.g. lark_oapi.ws) grab a long-lived reference to the loop;
            # a previous disconnect/failure may leave half-finished tasks on the old loop,
            # or leave it in running/closed state, and reusing it hits "This event loop is
            # already running" again. Create per round + close when done, combined with
            # adapter.prepare_ws_thread binding this round's loop into the SDK (the lark
            # adapter replaces the SDK's module-level global loop with a per-thread proxy →
            # multiple bots get one loop per thread, no overwriting).
            thread_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(thread_loop)
            worker.loop = thread_loop  # exposed to stop_connection for call_soon_threadsafe(stop)
            try:
                conn = self._load_conn(worker.channel_id)
                if conn is None or not conn.enabled:
                    worker.stop_flag.set()
                    break
                # Constructing the client does not validate app_id/secret, and lark ws
                # silently reconnects internally on auth failure without raising — we must
                # actively verify credentials once before marking connected, otherwise bad
                # credentials would stay stuck at connected forever.
                # A failure raises → falls through to the backoff retry below (credentials
                # may recover later, unlike an unrecoverable construction failure).
                self._verify_credentials(adapter, conn)
                client = self._build_client(worker, adapter, conn)
                if client is None:
                    self._set_status(worker.channel_id, "error", "适配器/SDK 不可用")
                    return  # unrecoverable, exit the thread
                worker.client = client
                # Bind this thread's loop into the SDK (lark isolated via thread-local proxy), then start
                prepare = getattr(adapter, "prepare_ws_thread", None)
                if prepare is not None:
                    prepare(thread_loop)
                self._set_status(worker.channel_id, "connected")
                backoff = 1.0
                client.start()  # blocking: SDK maintains the connection internally + auto-reconnects
            except Exception as exc:  # noqa: BLE001
                if worker.stop_flag.is_set():
                    break
                logger.warning(
                    "[channels] 长连接断开 channel_id=%s，%.0fs 后重试: %s",
                    worker.channel_id, backoff, exc,
                )
                self._set_status(worker.channel_id, "error", str(exc)[:480])
                time.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
            finally:
                # Close this round's loop (a successful start blocks above and never gets here;
                # only reached on disconnect/exception exit).
                try:
                    thread_loop.close()
                except Exception:  # noqa: BLE001
                    pass
        logger.info("[channels] 长连接线程退出 channel_id=%s", worker.channel_id)

    @staticmethod
    def _load_conn(channel_id: str):
        """Load and detach conn from the session before returning (credentials live in the config dict and remain readable after detaching)."""
        from core.db.engine import SessionLocal
        from core.db.repository.channel import ChannelConnectionRepository

        with SessionLocal() as db:
            conn = ChannelConnectionRepository(db).get_by_id(channel_id)
            if conn is not None:
                db.expunge(conn)
            return conn

    def _build_client(self, worker: _Worker, adapter, conn):
        make_ws = getattr(adapter, "make_ws_client", None)
        if make_ws is None:
            return None
        try:
            return make_ws(conn, self._dispatch_factory(worker.channel_id))
        except Exception as exc:  # noqa: BLE001 — construction failure (missing SDK, etc.) = unrecoverable, stop retrying
            logger.warning("[channels] 长连接 client 构造失败，停止重试 %s: %s", worker.channel_id, exc)
            return None

    def _verify_credentials(self, adapter, conn) -> None:
        """Actively verify credentials; raise on failure (_run_worker records error + backoff retry).

        validate_credentials is async and the worker runs on a separate thread, so it is
        submitted to the main loop for execution with a timeout.
        ``_loop`` is guaranteed by start_connection to be ready before the thread is
        started, so no extra None check is needed.
        """
        fut = asyncio.run_coroutine_threadsafe(
            adapter.validate_credentials(conn), self._loop
        )
        fut.result(timeout=15)

    def _dispatch_factory(self, channel_id: str):
        """Return a synchronous callback that delivers InboundMsg to handle_inbound on the main loop."""
        from core.channels.inbound import handle_inbound

        loop = self._loop

        def _dispatch(inbound) -> None:
            if loop is None or loop.is_closed():
                return
            asyncio.run_coroutine_threadsafe(handle_inbound(inbound), loop)

        return _dispatch

    # ── Utilities ───────────────────────────────────────────────────────
    @staticmethod
    def _safe_disconnect(worker: _Worker) -> None:
        # 1) Wake the worker blocked in client.start(): stop its event loop from an
        #    external thread. lark ws's start() blocks forever in run_until_complete(
        #    _select()); merely setting stop_flag or calling the async _disconnect cannot
        #    interrupt it → after the old connection is deleted/unbound the worker becomes
        #    a zombie, keeps holding the ws, steals messages of the same feishu app and
        #    routes them to the deleted channel_id ("inbound has no matching enabled
        #    connection"), and the user still receives nothing after re-binding. Stopping
        #    the loop makes start() return → _run_worker sees stop_flag and exits.
        loop = worker.loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:  # noqa: BLE001
                pass
        # 2) Synchronous stop/close (e.g. weixin _ILinkPoller.stop). Skip coroutine
        #    functions: we cannot await here, and calling them only triggers "coroutine
        #    never awaited" without actually disconnecting (lark._disconnect is exactly that).
        client = worker.client
        if client is None:
            return
        for name in ("stop", "close", "disconnect", "_disconnect"):
            fn = getattr(client, name, None)
            if callable(fn) and not asyncio.iscoroutinefunction(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
                return

    @staticmethod
    def _set_status(channel_id: str, status: str, last_error: Optional[str] = None) -> None:
        from core.db.engine import SessionLocal
        from core.db.repository.channel import ChannelConnectionRepository

        try:
            with SessionLocal() as db:
                ChannelConnectionRepository(db).set_status(
                    channel_id, status, last_error=last_error
                )
        except Exception:  # noqa: BLE001
            logger.debug("[channels] 状态回写失败 %s", channel_id, exc_info=True)


# Process-level singleton (used by lifespan startup + the service when scheduling bind/unbind)
_manager: Optional[ChannelManager] = None


def get_manager() -> ChannelManager:
    global _manager
    if _manager is None:
        _manager = ChannelManager()
    return _manager
