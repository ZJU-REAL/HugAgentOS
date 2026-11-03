"""Slack Socket Mode receiver (the long-connection transport for the Slack adapter).

Protocol: ``apps.connections.open`` with the app-level token (``xapp-``) returns a
one-off ``wss://`` URL. The server sends ``hello``, then envelopes
``{envelope_id, type, payload}`` that must each be acknowledged by echoing
``{"envelope_id": ...}`` within 3 seconds, or Slack redelivers. A ``disconnect`` frame
(connection refresh, roughly hourly) means: open a fresh URL and carry on.

``start()`` blocks on the manager's worker thread; ``stop()`` is called from another
thread and closes the socket so the blocking ``recv`` returns. Transport failures
propagate to the manager, which records the error and retries with backoff.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable, Optional

import httpx

from integrations.channels.adapters.slack_settings import SLACK_API_BASE
from integrations.channels.protocol import InboundMsg

if TYPE_CHECKING:
    from integrations.channels.adapters.slack import SlackAdapter

logger = logging.getLogger(__name__)

_RECV_POLL_SECONDS = 5.0


def open_socket_url(app_token: str) -> str:
    resp = httpx.post(
        f"{SLACK_API_BASE}/apps.connections.open",
        headers={"Authorization": f"Bearer {app_token}"},
        timeout=15,
    )
    data = resp.json() if resp.content else {}
    if not data.get("ok") or not data.get("url"):
        raise RuntimeError(f"Slack Socket Mode 连接失败: {data.get('error') or resp.status_code}")
    return data["url"]


class SlackSocketClient:
    def __init__(self, adapter: "SlackAdapter", conn: Any, app_token: str,
                 on_message: Callable[[InboundMsg], None]):
        self._adapter = adapter
        self._conn = conn
        self._app_token = app_token
        self._on_message = on_message
        self._stopped = threading.Event()
        self._ws = None
        self._on_connected: Optional[Callable[[], None]] = None

    def set_status_handler(self, on_connected: Callable[[], None]) -> None:
        self._on_connected = on_connected

    def start(self) -> None:
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect

        while not self._stopped.is_set():
            url = open_socket_url(self._app_token)
            with connect(url, open_timeout=15, max_size=None) as ws:
                self._ws = ws
                try:
                    if self._serve(ws):
                        return
                except ConnectionClosed:
                    if self._stopped.is_set():
                        return
                    raise RuntimeError("Slack Socket Mode 连接被关闭") from None
                finally:
                    self._ws = None

    def _serve(self, ws) -> bool:
        """Pump frames until Slack asks for a refresh (False) or stop() is called (True)."""
        while not self._stopped.is_set():
            try:
                raw = ws.recv(timeout=_RECV_POLL_SECONDS)
            except TimeoutError:
                continue
            frame = json.loads(raw) if isinstance(raw, (str, bytes)) else {}
            kind = frame.get("type")
            if kind == "hello":
                if self._on_connected is not None:
                    self._on_connected()
                continue
            if kind == "disconnect":
                logger.info("[slack] Socket Mode 刷新连接 reason=%s", frame.get("reason"))
                return False
            envelope_id = frame.get("envelope_id")
            if envelope_id:
                # Ack first: the 3s deadline must not depend on how long parsing takes.
                ws.send(json.dumps({"envelope_id": envelope_id}))
            if kind == "events_api":
                self._dispatch(frame.get("payload") or {})
        return True

    def _dispatch(self, payload: dict) -> None:
        try:
            inbound = self._adapter.parse_inbound(self._conn, payload)
            if inbound is not None:
                self._on_message(inbound)
        except Exception:  # noqa: BLE001
            logger.exception("[slack] 事件处理失败 channel_id=%s", self._conn.channel_id)

    def stop(self) -> None:
        self._stopped.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
