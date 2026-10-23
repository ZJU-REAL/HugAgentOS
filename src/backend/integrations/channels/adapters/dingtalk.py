"""DingTalk inbound channel adapter.

- **Binding validation**: exchange AppKey/AppSecret for an access_token
  (DingTalk v1.0 OAuth) — obtaining one is treated as valid credentials.
- **Inbound (Stream long connection)**: WebSocket via the official
  ``dingtalk-stream`` SDK, listening for bot message callbacks
  (``ChatbotMessage.TOPIC``). Zero public IP, no callback configuration. If the
  SDK is missing, this channel is unavailable but the process is unaffected.
- **Outbound replies**: use the ``sessionWebhook`` carried by each inbound
  message (no access_token needed), unified for p2p / group.
  Agent replies go through ``send_markdown`` as ``msgtype=markdown`` (DingTalk
  natively renders headings/bold/links/lists, etc.; tables/code fences do not
  render on mobile, so content is downgraded via
  ``core.channels.markdown.downgrade_for_dingtalk`` before sending).
  Short system messages (placeholders/receipts) still use ``send_text`` (plain text).
  DingTalk bots **do not support editing messages** → ``edit_message`` returns
  failure; but they do support **silent recall** of messages sent via the robot
  API (group ``groupMessages/recall`` / one-to-one ``otoMessages/batchRecall``,
  using the processQueryKey returned at send time). So placeholder messages go
  through ``send_placeholder`` via the robot API (obtaining a recallable key),
  and before the final reply the upper layer calls ``recall_message`` to remove
  the placeholder — visually equivalent to "replace". When the robot API is
  unavailable (no permission, etc.), placeholders automatically fall back to
  sessionWebhook plain text (not recallable, same behavior as the old version).
- **Outbound proactive delivery (automation scheduled tasks, etc., no inbound
  message)**: synthetic messages have no sessionWebhook → ``send_text`` /
  ``send_markdown`` automatically fall back to the robot API (``sampleText`` /
  ``sampleMarkdown``).
- **Outbound files**: sessionWebhook only accepts text-type messages, cannot
  send files → ``push_file`` uses the robot API: first ``media/upload`` to get
  a mediaId, then send a robot message. The robot message picks its endpoint by
  conversation type: group ``groupMessages/send`` (openConversationId);
  one-to-one with a staffId uses ``oToMessages/batchSend``, without one
  (proactive delivery) uses the human-bot conversation's openConversationId via
  ``privateChatMessages/send``.
  The app must be granted the "internal enterprise robot send message"
  permission on the DingTalk open platform, otherwise the send endpoints return 403.

Conversation keying (``external_conversation_id``) always uses DingTalk ``conversationId``:
  - one-to-one (conversationType==``1``) conversationId maps one-to-one to a user → naturally one conversation per person;
  - group (conversationType==``2``) conversationId is unique per group → the whole group shares one conversation.

Isomorphic to the [[lark]] adapter. See internal design docs.
"""

from __future__ import annotations
from integrations.channels.adapters.dingtalk_history import DingTalkHistory
from integrations.channels.adapters.dingtalk_delivery import DingTalkDelivery

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional

import httpx

from datetime import datetime, timezone

from integrations.channels.markdown import derive_title, downgrade_for_dingtalk
from integrations.channels.protocol import ChannelCaps, ChannelResourceError, HistoryItem, InboundMsg, SendResult, chunk_text
from core.infra.crypto import decrypt_secret

logger = logging.getLogger(__name__)

from integrations.channels.adapters.dingtalk_settings import DINGTALK_API_BASE


class DingTalkAdapter(DingTalkHistory, DingTalkDelivery):
    caps = ChannelCaps(
        channel_type="dingtalk",
        max_message_len=4000,
        supports_markdown=True,
        splits_long_messages=False,
        supports_long_conn=True,
        # DingTalk exposes ``isInAtList`` on every bot callback, so the gate is already
        # wired; whether non-@ group messages ever get delivered depends on the tenant
        # holding DingTalk's group-message read permission (applied for and reviewed on the
        # open platform). Without it, group listening stays inert rather than broken.
        supports_group_observe=True,
        bind_mode="credentials",
        credential_fields=("app_id", "app_secret"),
    )

    # access_token cache: {app_id: (token, expire_epoch)} (only for validate/testing; replies use sessionWebhook)
    _token_cache: Dict[str, tuple] = {}

    # ── Credentials ─────────────────────────────────────────────────────
    @staticmethod
    def _app_secret(conn: Any) -> str:
        cfg = conn.config if isinstance(conn.config, dict) else {}
        return decrypt_secret(cfg.get("app_secret_enc")) or ""

    async def _access_token(self, app_id: str, app_secret: str) -> str:
        cached = self._token_cache.get(app_id)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        url = f"{DINGTALK_API_BASE}/v1.0/oauth2/accessToken"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"appKey": app_id, "appSecret": app_secret})
        data = resp.json()
        token = data.get("accessToken")
        if not token:
            raise RuntimeError(f"钉钉 access_token 获取失败: {data}")
        self._token_cache[app_id] = (token, time.time() + int(data.get("expireIn", 7000)))
        return token

    async def validate_credentials(self, conn: Any) -> Dict[str, Any]:
        secret = self._app_secret(conn)
        if not conn.app_id or not secret:
            raise RuntimeError("缺少 AppKey / AppSecret")
        await self._access_token(conn.app_id, secret)
        return {"app_id": conn.app_id}

    # ── Webhook verification (DingTalk uses the Stream long connection; no webhook entry point) ──
    def verify_webhook(self, conn: Any, headers: Dict[str, str], body: bytes) -> bool:
        return True

    # ── Event → InboundMsg (the dict from the Stream callback) ──────────
    def parse_inbound(self, conn: Any, payload: Dict[str, Any]) -> Optional[InboundMsg]:
        """DingTalk bot callback data (dict) → InboundMsg. Returns None for non-text/unsupported messages."""
        msgtype = payload.get("msgtype")
        text = ""
        if msgtype == "text":
            text = ((payload.get("text") or {}).get("content") or "").strip()
        elif msgtype == "richText":
            # Rich text: concatenate the plain-text nodes inside
            parts = (payload.get("content") or {}).get("richText") or []
            text = " ".join(p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")).strip()
        attachments = self._extract_attachments(msgtype, payload)
        if not text and not attachments:
            return None
        conv_type = str(payload.get("conversationType") or "1")
        chat_type = "group" if conv_type == "2" else "p2p"
        conv_id = payload.get("conversationId") or ""
        return InboundMsg(
            channel_id=conn.channel_id,
            channel_type="dingtalk",
            text=text,
            chat_type=chat_type,
            external_conversation_id=conv_id,
            sender_id=payload.get("senderStaffId") or payload.get("senderId") or "",
            sender_name=payload.get("senderNick") or "",
            message_id=payload.get("msgId") or "",
            attachments=attachments,
            # ``isInAtList`` is DingTalk's own "was the bot in the @ list" flag. With only the
            # default permission DingTalk delivers @bot messages exclusively, so this is
            # always true and the gate is a no-op; once the tenant is granted group-message
            # read, non-@ messages start arriving with it false and silent observation kicks
            # in with no further code change. Absent field → treat as addressed.
            addressed_to_bot=(
                True if chat_type == "p2p" else bool(payload.get("isInAtList", True))
            ),
            raw={
                "dingtalk_session_webhook": payload.get("sessionWebhook") or "",
                "dingtalk_conversation_id": conv_id,
                # Needed by download_resource: messageFiles/download requires robotCode, and
                # per DingTalk's docs it is **not** the AppKey — the only reliable source is
                # this field on the callback payload (present in Stream mode, which is what
                # the long connection uses).
                "dingtalk_robot_code": payload.get("robotCode") or "",
            },
        )

    # ── Group history pull (via the official dws CLI) ────────────────────
    # DingTalk has **no** REST OpenAPI for reading a group's message history — the
    # server-side API list only covers sending, recalling and read receipts. The capability
    # is reachable two ways, and this takes the second:
    #   1. DingTalk's MCP gateway (`list_conversation_message_v2` on the group-chat MCP
    #      server). Works, but needs credentials provisioned per app — extra setup.
    #   2. The official `dws` CLI, which the backend **already** runs as a subprocess for
    #      this user's DingTalk connection and which is **already logged in**. No new
    #      credentials, no extra configuration: it simply reuses the connection the owner
    #      has set up. That is why this is the one implemented.
    #
    # Runs as the connection's owner (per-user $HOME is the credential isolation boundary,
    # same as every other backend dws call). The 8s cap keeps a slow CLI from eating into
    # the caller's own history budget; any failure returns [] and history is skipped.
    _DWS_HISTORY_TIMEOUT_S = 8


    _DWS_DOWNLOAD_TIMEOUT_S = 45





    # dws renders a file message as **plain text**, not a structured attachment:
    #   "[文件] 周报.docx fileId: X6GRez... 注意：如需下载使用dws drive download命令下载"
    # Parsed back out here, otherwise a pulled file is just an unusable sentence — the agent
    # can see a file was shared but has no handle to open it. Images use the same shape.
    _DWS_FILE_RE = re.compile(
        r"\[(?P<kind>文件|图片)\]\s*(?P<name>.+?)\s+fileId:\s*(?P<fid>[A-Za-z0-9_\-]+)"
        r"(?:\s*注意[：:][^\n]*)?"
    )

    # A DingTalk **online document** shared into a group arrives as a link card, not as a
    # file: a title line, an icon, and an alidocs URL. It is a different resource from a Drive
    # file — read with `dws doc read`, not downloaded by fileId — and without recognising it
    # the agent sees only an unusable link.
    _DWS_DOC_RE = re.compile(r"https?://alidocs\.dingtalk\.com/i/nodes/(?P<node>[A-Za-z0-9_\-]+)")




    # ── Inbound attachments ─────────────────────────────────────────────


    # ── Outbound push (via sessionWebhook, no access_token needed) ───────



    # Lets the upper layer do a whole-content downgrade **before** chunking
    # (avoids a table getting bisected by a chunk boundary and becoming unrecognizable for conversion).
    prepare_markdown = staticmethod(downgrade_for_dingtalk)




    # ── Placeholder messages: sent via robot API (recallable) → recall as an equivalent of "replace" ──



    # ── Outbound file delivery (sessionWebhook cannot send files → use the robot API) ──



    # ── Long connection (Stream, requires the dingtalk-stream SDK) ──────
    def make_ws_client(self, conn: Any, on_message: Callable[[InboundMsg], None]) -> Any:
        """Build the DingTalk Stream long-connection runner. ``on_message`` is invoked synchronously on the SDK thread (dispatched to the main loop).

        dingtalk_stream is lazily imported; if not installed a RuntimeError is
        raised, which the manager records as an error without affecting the
        process. The SDK's ``start()`` is async and ``start_forever()`` cannot
        be stopped → wrap it in a controllable runner so the manager can start
        and stop cleanly (actually disconnecting when a bot is disabled).
        """
        try:
            import dingtalk_stream
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("dingtalk-stream SDK 未安装，长连接不可用") from exc

        adapter = self
        app_id = conn.app_id
        secret = self._app_secret(conn)

        class _Handler(dingtalk_stream.ChatbotHandler):
            async def process(self, callback):  # type: ignore[override]
                try:
                    inbound = adapter.parse_inbound(conn, callback.data or {})
                    if inbound is not None:
                        on_message(inbound)
                except Exception:  # noqa: BLE001
                    logger.exception("[dingtalk] Stream 事件处理失败 channel_id=%s", conn.channel_id)
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"

        def _build_client():
            sdk_logger = logging.getLogger("dingtalk_stream")
            sdk_logger.setLevel(logging.WARNING)
            credential = dingtalk_stream.Credential(app_id, secret)
            client = dingtalk_stream.DingTalkStreamClient(credential, logger=sdk_logger)
            client.register_callback_handler(
                dingtalk_stream.chatbot.ChatbotMessage.TOPIC, _Handler()
            )
            return client

        return _DingTalkStreamRunner(_build_client)


class _DingTalkStreamRunner:
    """Wraps dingtalk-stream's async ``client.start()`` into the "blocking start() + stoppable stop()" the manager expects.

    Creates a dedicated event loop in the worker thread to run ``client.start()``
    (the SDK reconnects on its own); ``stop()`` closes the websocket and cancels
    the task from the main thread via ``call_soon_threadsafe``, making
    ``start()`` return → the manager's thread body sees stop_flag set and exits
    cleanly.
    """

    def __init__(self, build_client: Callable[[], Any]):
        self._build_client = build_client
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client: Any = None
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._client = self._build_client()
            self._task = loop.create_task(self._client.start())
            loop.run_until_complete(self._task)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass
            loop.close()

    def stop(self) -> None:
        loop, task, client = self._loop, self._task, self._client
        if loop is None or loop.is_closed():
            return

        def _cancel() -> None:
            ws = getattr(client, "websocket", None)
            if ws is not None:
                try:
                    asyncio.ensure_future(ws.close())
                except Exception:  # noqa: BLE001
                    pass
            if task is not None:
                task.cancel()

        try:
            loop.call_soon_threadsafe(_cancel)
        except Exception:  # noqa: BLE001
            pass
