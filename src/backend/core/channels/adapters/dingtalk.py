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

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, Optional

import httpx

from core.channels.markdown import derive_title, downgrade_for_dingtalk
from core.channels.protocol import ChannelCaps, InboundMsg, SendResult, chunk_text
from core.infra.crypto import decrypt_secret

logger = logging.getLogger(__name__)

DINGTALK_API_BASE = "https://api.dingtalk.com"


class DingTalkAdapter:
    caps = ChannelCaps(
        channel_type="dingtalk",
        max_message_len=4000,
        supports_markdown=True,
        splits_long_messages=False,
        supports_long_conn=True,
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
        if not text:
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
            attachments=[],
            raw={
                "dingtalk_session_webhook": payload.get("sessionWebhook") or "",
                "dingtalk_conversation_id": conv_id,
            },
        )

    # ── Outbound push (via sessionWebhook, no access_token needed) ───────
    @staticmethod
    def _session_webhook(inbound: InboundMsg) -> str:
        return (inbound.raw or {}).get("dingtalk_session_webhook") or ""

    async def _post_webhook(self, webhook: str, payload: Dict[str, Any]) -> SendResult:
        if not webhook:
            return SendResult.fail("forbidden", "缺少 sessionWebhook（可能已过期）")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(webhook, json=payload)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", str(exc))
        if data.get("errcode", 0) == 0:
            return SendResult.ok()
        kind = "rate_limited" if data.get("errcode") in (130101, 88) else "unknown"
        return SendResult.fail(kind, f"errcode={data.get('errcode')} {data.get('errmsg')}")

    async def send_text(self, conn: Any, inbound: InboundMsg, text: str) -> SendResult:
        webhook = self._session_webhook(inbound)
        if webhook:
            return await self._post_webhook(
                webhook, {"msgtype": "text", "text": {"content": text}}
            )
        # Proactive delivery (automation scheduled tasks, etc.) has no inbound sessionWebhook → use the robot API
        try:
            token = await self._access_token(conn.app_id, self._app_secret(conn))
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", str(exc))
        return await self._robot_send(token, conn, inbound, "sampleText", {"content": text})

    # Lets the upper layer do a whole-content downgrade **before** chunking
    # (avoids a table getting bisected by a chunk boundary and becoming unrecognizable for conversion).
    prepare_markdown = staticmethod(downgrade_for_dingtalk)

    async def send_markdown(self, conn: Any, inbound: InboundMsg, text: str) -> SendResult:
        """Send a DingTalk markdown message (natively rendered). The downgrade is idempotent; calling directly or via prepare_markdown is equally safe.

        title is required for DingTalk's markdown type; it shows in the
        conversation-list summary / push notification and is derived from the
        first line of the body.
        """
        md = downgrade_for_dingtalk(text)
        title = derive_title(md)
        webhook = self._session_webhook(inbound)
        if webhook:
            return await self._post_webhook(
                webhook, {"msgtype": "markdown", "markdown": {"title": title, "text": md}}
            )
        # Proactive delivery → robot-API markdown message
        try:
            token = await self._access_token(conn.app_id, self._app_secret(conn))
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", str(exc))
        return await self._robot_send(
            token, conn, inbound, "sampleMarkdown", {"title": title, "text": md}
        )

    async def push(self, conn: Any, inbound: InboundMsg, content: str) -> SendResult:
        md = downgrade_for_dingtalk(content)
        chunks = chunk_text(md, self.caps.max_message_len) or [md]
        first: Optional[SendResult] = None
        for i, c in enumerate(chunks):
            r = await self.send_markdown(conn, inbound, c)
            if i == 0:
                first = r
                if not r.success:
                    return r
        return first or SendResult.fail("unknown", "空内容")

    async def edit_message(self, conn: Any, message_id: str, text: str) -> SendResult:
        # DingTalk bots cannot edit messages — the upper-layer placeholder logic
        # therefore switches to "recall + resend" (see recall_message).
        return SendResult.fail("bad_format", "钉钉不支持编辑消息")

    # ── Placeholder messages: sent via robot API (recallable) → recall as an equivalent of "replace" ──
    async def send_placeholder(self, conn: Any, inbound: InboundMsg, text: str) -> SendResult:
        """Send a recallable placeholder message via the robot API; message_id is the processQueryKey (recall credential).

        If the robot API is unavailable (no permission / token failure) → fall
        back to sessionWebhook plain text; then there is no message_id and the
        upper layer will not attempt recall — behavior degrades to the old
        version (placeholder stays, reply is sent fresh).
        """
        try:
            token = await self._access_token(conn.app_id, self._app_secret(conn))
            r = await self._robot_send(token, conn, inbound, "sampleText", {"content": text})
            if r.success:
                return r
        except Exception:  # noqa: BLE001
            logger.debug("[dingtalk] 机器人占位发送失败，回退 webhook", exc_info=True)
        return await self.send_text(conn, inbound, text)

    def _recall_url_body(
        self, conn: Any, inbound: InboundMsg, message_id: str
    ) -> Optional[tuple]:
        """Pick the recall endpoint by conversation type. Group chats need openConversationId; one-to-one batchRecall needs only the key."""
        body: Dict[str, Any] = {"robotCode": conn.app_id, "processQueryKeys": [message_id]}
        if inbound.chat_type == "group":
            conv_id = inbound.external_conversation_id or (inbound.raw or {}).get("dingtalk_conversation_id") or ""
            if not conv_id:
                return None
            body["openConversationId"] = conv_id
            return f"{DINGTALK_API_BASE}/v1.0/robot/groupMessages/recall", body
        return f"{DINGTALK_API_BASE}/v1.0/robot/otoMessages/batchRecall", body

    async def recall_message(self, conn: Any, inbound: InboundMsg, message_id: str) -> SendResult:
        """Silently recall a message sent via the robot API (the client shows no recall notice)."""
        if not message_id:
            return SendResult.fail("bad_format", "缺少 processQueryKey")
        target = self._recall_url_body(conn, inbound, message_id)
        if target is None:
            return SendResult.fail("bad_format", "缺少 openConversationId")
        url, body = target
        try:
            token = await self._access_token(conn.app_id, self._app_secret(conn))
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, headers={"x-acs-dingtalk-access-token": token}, json=body)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", str(exc))
        if resp.status_code == 200:
            return SendResult.ok(message_id)
        return SendResult.fail("unknown", f"撤回失败: {resp.status_code} {str(data)[:200]}")

    # ── Outbound file delivery (sessionWebhook cannot send files → use the robot API) ──
    async def push_file(
        self, conn: Any, inbound: InboundMsg, content: bytes, filename: str, mime_type: str
    ) -> SendResult:
        secret = self._app_secret(conn)
        if not conn.app_id or not secret:
            return SendResult.fail("forbidden", "缺少 AppKey / AppSecret")
        try:
            token = await self._access_token(conn.app_id, secret)
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", str(exc))

        is_image = (mime_type or "").startswith("image/")
        media_id = await self._upload_media(token, content, filename, "image" if is_image else "file")
        if isinstance(media_id, SendResult):
            return media_id

        if is_image:
            # sampleImageMsg's photoURL accepts a mediaId directly
            msg_key, msg_param = "sampleImageMsg", {"photoURL": media_id}
        else:
            ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower() or "file"
            msg_key = "sampleFile"
            msg_param = {"mediaId": media_id, "fileName": filename, "fileType": ext}
        return await self._robot_send(token, conn, inbound, msg_key, msg_param)

    async def _upload_media(self, token: str, content: bytes, filename: str, media_type: str):
        """Upload media in exchange for a mediaId; returns SendResult on failure (str on success)."""
        url = f"https://oapi.dingtalk.com/media/upload?access_token={token}&type={media_type}"
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, files={"media": (filename, content)})
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", f"媒体上传失败: {exc}")
        media_id = data.get("media_id")
        if data.get("errcode", 0) != 0 or not media_id:
            return SendResult.fail("unknown", f"媒体上传失败: errcode={data.get('errcode')} {data.get('errmsg')}")
        return media_id

    async def _robot_send(
        self, token: str, conn: Any, inbound: InboundMsg, msg_key: str, msg_param: Dict[str, Any]
    ) -> SendResult:
        """Robot proactive message: group chats use openConversationId, one-to-one uses the recipient's staffId."""
        body: Dict[str, Any] = {
            "robotCode": conn.app_id,  # for internal enterprise apps, robotCode == appKey
            "msgKey": msg_key,
            "msgParam": json.dumps(msg_param, ensure_ascii=False),
        }
        if inbound.chat_type == "group":
            conv_id = inbound.external_conversation_id or (inbound.raw or {}).get("dingtalk_conversation_id") or ""
            if not conv_id:
                return SendResult.fail("bad_format", "缺少 openConversationId")
            url = f"{DINGTALK_API_BASE}/v1.0/robot/groupMessages/send"
            body["openConversationId"] = conv_id
        else:
            staff_id = inbound.sender_id or ""
            if staff_id:
                url = f"{DINGTALK_API_BASE}/v1.0/robot/oToMessages/batchSend"
                body["userIds"] = [staff_id]
            else:
                # In proactive-delivery scenarios no staffId is available → send directly via the human-bot one-to-one conversation's openConversationId
                conv_id = inbound.external_conversation_id or (inbound.raw or {}).get("dingtalk_conversation_id") or ""
                if not conv_id:
                    return SendResult.fail("bad_format", "缺少接收人 staffId / openConversationId")
                url = f"{DINGTALK_API_BASE}/v1.0/robot/privateChatMessages/send"
                body["openConversationId"] = conv_id
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(url, headers={"x-acs-dingtalk-access-token": token}, json=body)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", str(exc))
        key = data.get("processQueryKey") or data.get("processQueryKeys")
        if resp.status_code == 200 and key:
            # The processQueryKey is the recall credential, returned to the upper layer as message_id (used by recall_message)
            if isinstance(key, list):
                key = key[0] if key else None
            return SendResult.ok(key if isinstance(key, str) else None)
        code = data.get("code") or resp.status_code
        kind = "forbidden" if resp.status_code == 403 else "unknown"
        return SendResult.fail(kind, f"机器人消息发送失败: {code} {str(data)[:200]}")

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
