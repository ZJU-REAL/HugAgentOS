"""Feishu (Lark) inbound channel adapter.

- **Binding validation / outbound push**: Feishu OpenAPI REST (httpx) with a
  tenant_access_token; no SDK needed.
- **Webhook decryption/signature verification**: AES-256-CBC (Encrypt Key) +
  signature check, pure cryptography; no SDK needed.
- **Long connection (WebSocket)**: uses the official ``lark_oapi`` SDK's
  ws.Client (lazy import; if the SDK is missing only the long connection is
  unavailable, webhook/push are unaffected). Long connection is recommended in
  production: the user only needs App ID/Secret, no public callback URL.

Conversation keying (``external_conversation_id``) always uses Feishu
``message.chat_id``:
  - a p2p chat_id maps one-to-one to a user → naturally one conversation per DM peer;
  - a group chat_id is unique per group → the whole group shares one conversation.
Outbound push also sends by chat_id (``receive_id_type=chat_id``), unified for p2p/group.

See internal design docs.
"""

from __future__ import annotations
from integrations.channels.adapters.lark_messages import LarkMessages
from integrations.channels.adapters.lark_delivery import LarkDelivery

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import httpx

from integrations.channels.protocol import ChannelCaps, HistoryItem, InboundMsg, SendResult
from core.infra.crypto import decrypt_secret

logger = logging.getLogger(__name__)

from integrations.channels.adapters.lark_settings import LARK_API_BASE
_MENTION_RE = re.compile(r"@_user_\d+")


# ── lark_oapi ws event-loop isolation (supports multiple coexisting bots) ────
# Root cause: ``lark_oapi/ws/client.py`` runs everything on a single
# **module-level global** ``loop`` (``loop.run_until_complete`` in ``start()``,
# ``loop.create_task`` in ``_connect`` ...). Each bot's long connection runs in
# its own worker thread, but they all share that one global → when the second
# bot starts it overwrites the global loop with its own thread's loop, and the
# first bot's already-running coroutines then call ``loop.create_task`` on
# someone else's loop → "This event loop is already running" / cross-thread
# crashes. Single-bot setups hit the same cause sporadically (the global gets
# rewritten on reconnect/retry).
# Fix: replace that global ``loop`` with a **per-thread-resolving proxy** — each
# worker thread always resolves to its own thread's loop (prepare_ws_thread
# writes it into thread-local storage), fully eliminating cross-thread sharing
# and overwriting.
_ws_loop_tls = threading.local()
_ws_proxy_installed = False
_ws_proxy_lock = threading.Lock()


class _PerThreadLoopProxy:
    """Masquerades as an event loop: every attribute/method access is forwarded to the real loop bound to the **current thread**."""

    def __getattr__(self, name: str) -> Any:
        loop = getattr(_ws_loop_tls, "loop", None)
        if loop is None:
            loop = asyncio.get_event_loop()
        return getattr(loop, name)


def _install_ws_loop_proxy() -> None:
    """One-time replacement of lark_oapi.ws.client's module-level global loop with the per-thread proxy (idempotent)."""
    global _ws_proxy_installed
    if _ws_proxy_installed:
        return
    with _ws_proxy_lock:
        if _ws_proxy_installed:
            return
        try:
            import lark_oapi.ws.client as _wsc
            _wsc.loop = _PerThreadLoopProxy()
            _ws_proxy_installed = True
        except Exception:  # noqa: BLE001 — if the SDK is missing, the long connection is unavailable anyway
            pass


class LarkAdapter(LarkMessages, LarkDelivery):
    caps = ChannelCaps(
        channel_type="lark",
        max_message_len=4000,        # Feishu's per-message text cap is fairly high; leave headroom for chunking
        supports_markdown=False,     # uses text messages; rich text (post) left for later
        splits_long_messages=False,
        supports_long_conn=True,
        # Feishu gates group delivery with three mutually distinct scopes:
        #   im:message.group_at_msg                        → only messages @-ing this bot
        #   im:message.group_at_msg.include_bot:readonly   → messages @-ing any bot
        #   im:message.group_msg (sensitive)               → every group message
        # Group listening needs the last one, plus republishing the app version; with the
        # others Feishu never pushes a non-@ message and observe_all stays inert. Note the
        # @-gate is worth running under the middle scope too: it stops us answering a message
        # that @'d somebody else's bot.
        supports_group_observe=True,
    )

    # tenant_access_token cache: {app_id: (token, expire_epoch)}
    _token_cache: Dict[str, tuple] = {}
    # The bot's own open_id: {app_id: open_id}. Stable per app → cached without expiry.
    _bot_id_cache: Dict[str, str] = {}

    # ── Credentials ─────────────────────────────────────────────────────
    @staticmethod
    def _app_secret(conn: Any) -> str:
        cfg = conn.config if isinstance(conn.config, dict) else {}
        return decrypt_secret(cfg.get("app_secret_enc")) or ""

    @staticmethod
    def _encrypt_key(conn: Any) -> str:
        cfg = conn.config if isinstance(conn.config, dict) else {}
        return decrypt_secret(cfg.get("encrypt_key_enc")) or ""

    @staticmethod
    def _verification_token(conn: Any) -> str:
        cfg = conn.config if isinstance(conn.config, dict) else {}
        return decrypt_secret(cfg.get("verification_token_enc")) or ""

    async def _tenant_token(self, app_id: str, app_secret: str) -> str:
        cached = self._token_cache.get(app_id)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        url = f"{LARK_API_BASE}/open-apis/auth/v3/tenant_access_token/internal"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"app_id": app_id, "app_secret": app_secret})
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"飞书 tenant_access_token 获取失败: {data.get('msg') or data}")
        token = data["tenant_access_token"]
        self._token_cache[app_id] = (token, time.time() + int(data.get("expire", 7000)))
        return token

    async def validate_credentials(self, conn: Any) -> Dict[str, Any]:
        """Successfully obtaining a token means the credentials are valid; returns an identity summary. Raises on failure."""
        secret = self._app_secret(conn)
        if not conn.app_id or not secret:
            raise RuntimeError("缺少 App ID / App Secret")
        await self._tenant_token(conn.app_id, secret)
        return {"app_id": conn.app_id}

    # ── Webhook decryption / signature verification ─────────────────────



    # ── Event → InboundMsg ──────────────────────────────────────────────







    # ── Group history pull (app credentials, independent of group listening) ──


    # ── Group listening: is this group message @'ing the bot? ───────────
    async def resolve_addressed(self, conn: Any, inbound: InboundMsg) -> bool:
        """True when the bot's own open_id appears in the message's mention list.

        Fails open (True) whenever the bot identity cannot be fetched, so a transient
        ``bot/v3/info`` failure degrades to today's behavior (answer everything delivered)
        rather than making the bot go mute in the group.
        """
        try:
            bot_open_id = await self._bot_open_id(conn)
        except Exception:  # noqa: BLE001
            logger.debug("[lark] 机器人 open_id 获取失败，按「已被 @」处理", exc_info=True)
            return True
        if not bot_open_id:
            return True
        return bot_open_id in (inbound.mentioned_ids or [])

    async def _bot_open_id(self, conn: Any) -> str:
        """The bot's own open_id (``/open-apis/bot/v3/info``), cached per app_id.

        Feishu never puts the bot identity in the event payload, so it has to be fetched once
        and kept. The value is stable for the lifetime of the app, hence a plain process cache
        with no expiry (a restart just re-fetches).
        """
        cached = self._bot_id_cache.get(conn.app_id)
        if cached:
            return cached
        token = await self._tenant_token(conn.app_id, self._app_secret(conn))
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{LARK_API_BASE}/open-apis/bot/v3/info",
                headers={"Authorization": f"Bearer {token}"},
            )
        open_id = ((resp.json() or {}).get("bot") or {}).get("open_id") or ""
        if open_id:
            self._bot_id_cache[conn.app_id] = open_id
        return open_id

    # ── Outbound push ───────────────────────────────────────────────────





    # ── Inbound file download ───────────────────────────────────────────

    # ── Outbound file delivery ──────────────────────────────────────────



    # ── Long connection (WebSocket, requires the lark_oapi SDK) ─────────
    def make_ws_client(self, conn: Any, on_message: Callable[[InboundMsg], None]) -> Any:
        """Build the Feishu long-connection client. ``on_message`` is invoked synchronously on the SDK thread; the caller dispatches it to the main loop.

        lark_oapi is lazily imported; if not installed a RuntimeError is raised,
        which the manager records as an error state without affecting the process.
        """
        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("lark_oapi SDK 未安装，长连接不可用") from exc

        secret = self._app_secret(conn)

        def _do_message(data: "P2ImMessageReceiveV1") -> None:
            try:
                inbound = self._inbound_from_sdk_event(conn, data)
                if inbound is not None:
                    on_message(inbound)
            except Exception:  # noqa: BLE001
                logger.exception("[lark] 长连接事件处理失败 channel_id=%s", conn.channel_id)

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_do_message)
            .build()
        )
        return lark.ws.Client(
            conn.app_id, secret, event_handler=handler, log_level=lark.LogLevel.WARNING
        )

    def prepare_ws_thread(self, loop: Any) -> None:
        """Called inside the worker thread: install the per-thread loop proxy + write this thread's loop into thread-local storage.

        From then on, every access to the global ``loop`` inside
        lark_oapi.ws.client is forwarded by the proxy to **this thread's** loop —
        multiple Feishu bots each get their own thread and their own loop with
        no mutual overwriting (fixes "This event loop is already running"). The
        proxy is installed only once (process-level idempotent); each thread
        writes its own thread-local value.
        """
        _install_ws_loop_proxy()
        _ws_loop_tls.loop = loop
