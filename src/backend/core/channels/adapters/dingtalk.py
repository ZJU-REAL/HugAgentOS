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
from typing import Any, Callable, Dict, List, Optional

import httpx

from datetime import datetime, timezone

from core.channels.markdown import derive_title, downgrade_for_dingtalk
from core.channels.protocol import (
    ChannelCaps,
    HistoryItem,
    InboundMsg,
    SendResult,
    chunk_text,
)
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
        # DingTalk exposes ``isInAtList`` on every bot callback, so the gate is already
        # wired; whether non-@ group messages ever get delivered depends on the tenant
        # holding DingTalk's group-message read permission (applied for and reviewed on the
        # open platform). Without it, group listening stays inert rather than broken.
        supports_group_observe=True,
        bind_mode="credentials",
        # mcp_* are optional and only needed for pulling group history through DingTalk's MCP
        # gateway (see fetch_history): either a user access token, or the service_id +
        # access_key pair issued when an MCP service is installed for the app. They flow in
        # through CreateBotRequest.extra and are encrypted into config like any other secret.
        credential_fields=(
            "app_id", "app_secret", "mcp_user_token", "mcp_service_id", "mcp_access_key",
        ),
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

    # ── Group history pull (DingTalk MCP gateway) ────────────────────────
    # DingTalk exposes no classic REST OpenAPI for reading a group's message history — the
    # server-side API list only covers sending, recalling and read-receipts. The capability
    # lives behind DingTalk's **MCP gateway** instead, as the ``list_conversation_message_v2``
    # tool on the "钉钉群聊" MCP server, which is why it cannot be found among the REST docs.
    #
    # Verified against the live gateway:
    #   POST https://mcp-gw.dingtalk.com/server/<serverId>
    #   JSON-RPC 2.0  method=tools/call  name=list_conversation_message_v2
    #   arguments {openconversation_id, time:"yyyy-MM-dd HH:mm:ss", limit, forward}
    #   → {"result": {"messages": [...], "hasMore": bool, "nextCursor": <epoch ms>}}
    #
    # Auth is the part that needs the tenant's own setup, which is why both accepted forms are
    # supported and neither is hardcoded:
    #   * ``x-user-access-token`` — a **user** identity token. Confirmed working end-to-end.
    #   * ``service_id`` + ``access_key`` — the service-level pair the gateway names when no
    #     token is supplied, issued when an MCP service is installed for an app. Sent in both
    #     query and headers because the exact placement is not publicly documented and the
    #     gateway ignores unknown extras.
    # Credentials come from the connection's own config, so this stays independent of the
    # `dws` CLI plugin: no subprocess, no sandbox, no reading another tool's keychain.
    DINGTALK_MCP_GATEWAY = "https://mcp-gw.dingtalk.com/server"
    # "钉钉群聊" MCP server id (market key 27f939aef74c67b5). Overridable per connection.
    DINGTALK_GROUP_CHAT_SERVER = (
        "0a1609437385696b77fc4771c3ddaf5656b487f809966c0cc8d4755e7b1d3b74"
    )

    def _mcp_auth(self, conn: Any) -> Optional[tuple]:
        """(url, headers) for the group-chat MCP server, or None when unconfigured."""
        cfg = conn.config if isinstance(conn.config, dict) else {}
        server = (cfg.get("mcp_group_chat_server") or self.DINGTALK_GROUP_CHAT_SERVER).strip()
        url = f"{self.DINGTALK_MCP_GATEWAY}/{server}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        user_token = decrypt_secret(cfg.get("mcp_user_token_enc")) or ""
        service_id = decrypt_secret(cfg.get("mcp_service_id_enc")) or ""
        access_key = decrypt_secret(cfg.get("mcp_access_key_enc")) or ""
        if user_token:
            headers["x-user-access-token"] = user_token
        elif service_id and access_key:
            headers["service_id"] = service_id
            headers["access_key"] = access_key
            url = f"{url}?service_id={service_id}&access_key={access_key}"
        else:
            return None  # not configured → the caller skips history entirely
        return url, headers

    async def fetch_history(
        self, conn: Any, conversation_id: str, *, since_ms: int, limit: int
    ) -> List[HistoryItem]:
        """Pull a group's messages via the MCP gateway. Returns [] on anything unexpected."""
        conv = (conversation_id or "").strip()
        auth = self._mcp_auth(conn)
        if not conv or auth is None:
            return []
        url, headers = auth
        # The tool takes a local-time string, not epoch ms.
        since_dt = datetime.fromtimestamp(max(since_ms, 0) / 1000, tz=timezone.utc).astimezone()
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {
                "name": "list_conversation_message_v2",
                "arguments": {
                    "openconversation_id": conv,
                    "time": since_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "limit": max(1, min(int(limit or 50), 100)),
                    "forward": True,   # messages *after* the cursor
                },
            },
        }
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                logger.warning(
                    "[dingtalk] 群历史拉取失败 status=%s body=%s",
                    resp.status_code, resp.text[:200],
                )
                return []
            return self._parse_mcp_history(resp.json() or {})
        except Exception:  # noqa: BLE001
            logger.warning("[dingtalk] 群历史拉取异常 conv=%s", conv[:24], exc_info=True)
            return []

    @staticmethod
    def _parse_mcp_history(body: Dict[str, Any]) -> List[HistoryItem]:
        """JSON-RPC envelope → HistoryItem list.

        The gateway wraps the tool payload in MCP's ``result.content[].text`` when it answers
        as a tool result, but answers some calls with the raw object; both shapes are handled
        rather than guessing which one a given deployment returns.
        """
        result = body.get("result")
        if isinstance(result, dict) and "content" in result:
            for block in result.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    try:
                        result = json.loads(block.get("text") or "{}")
                    except Exception:  # noqa: BLE001
                        result = {}
                    break
        if not isinstance(result, dict):
            return []
        inner = result.get("result") if isinstance(result.get("result"), dict) else result
        out: List[HistoryItem] = []
        for m in inner.get("messages") or []:
            if not isinstance(m, dict):
                continue
            # Written out rather than chained: `a or b if cond else ""` binds the conditional
            # looser than the `or`, which silently made every message look empty.
            text = m.get("content") or ""
            if not text and isinstance(m.get("text"), dict):
                text = (m["text"].get("content") or "")
            if not isinstance(text, str):
                text = str(text)
            sender = m.get("senderNick") or m.get("senderName") or m.get("senderId") or "群成员"
            try:
                ts = int(m.get("createTime") or m.get("createAt") or 0)
            except (TypeError, ValueError):
                ts = 0
            mid = str(m.get("msgId") or m.get("messageId") or "")
            if not text.strip():
                continue
            out.append(HistoryItem(
                message_id=mid, sender_name=str(sender), text=text.strip(), ts_ms=ts,
                attachments=[], raw={},
            ))
        return out

    # ── Inbound attachments ─────────────────────────────────────────────
    @staticmethod
    def _extract_attachments(msgtype: str, payload: Dict[str, Any]) -> list:
        """Message → attachment list. ``key`` carries DingTalk's ``downloadCode``.

        Content layout per DingTalk's "received message types" reference:
          picture  → {pictureDownloadCode, downloadCode}
          file     → {spaceId, fileName, downloadCode, fileId}
          audio    → {downloadCode, recognition}
          video    → {duration, videoType, downloadCode}
          richText → {richText: [ ...nodes, image nodes carry downloadCode... ]}
        Everything is read defensively (``downloadCode`` looked up in more than one place)
        because these payloads are only loosely specified and vary by client version — a
        missing field must degrade to "no attachment", never raise and kill the connection.
        """
        content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
        out: list = []

        def _add(kind: str, code: Any, name: str) -> None:
            if code:
                out.append({"kind": kind, "key": str(code), "name": name})

        if msgtype == "picture":
            code = content.get("downloadCode") or content.get("pictureDownloadCode")
            _add("image", code, f"dingtalk_image_{payload.get('msgId', '')[:8] or 'x'}.png")
        elif msgtype == "file":
            _add("file", content.get("downloadCode"), content.get("fileName") or "file.bin")
        elif msgtype == "audio":
            _add("file", content.get("downloadCode"), "dingtalk_audio.amr")
        elif msgtype == "video":
            _add("file", content.get("downloadCode"), "dingtalk_video.mp4")
        elif msgtype == "richText":
            for i, node in enumerate(content.get("richText") or []):
                if not isinstance(node, dict):
                    continue
                code = node.get("downloadCode") or node.get("pictureDownloadCode")
                _add("image", code, f"dingtalk_richtext_{i}.png")
        return out

    async def download_resource(
        self, conn: Any, inbound: InboundMsg, attachment: Dict[str, Any]
    ) -> Optional[bytes]:
        """Download an inbound attachment: downloadCode → temporary downloadUrl → bytes.

        Two hops, per DingTalk's design:
          POST /v1.0/robot/messageFiles/download {downloadCode, robotCode} → {downloadUrl}
          GET  downloadUrl → bytes
        The temporary URL serves the file with a ``.file`` extension; we ignore that and keep
        the filename taken from the message, so the artifact keeps its real extension.
        Download codes expire, so a deferred fetch can legitimately fail — returning None
        (rather than raising) lets the caller degrade to "attachment unavailable".
        """
        code = attachment.get("key")
        robot_code = (inbound.raw or {}).get("dingtalk_robot_code") or ""
        if not code:
            return None
        if not robot_code:
            logger.warning("[dingtalk] 缺少 robotCode，无法下载附件 code=%s", str(code)[:12])
            return None
        try:
            token = await self._access_token(conn.app_id, self._app_secret(conn))
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{DINGTALK_API_BASE}/v1.0/robot/messageFiles/download",
                    headers={"x-acs-dingtalk-access-token": token},
                    json={"downloadCode": code, "robotCode": robot_code},
                )
                if resp.status_code != 200:
                    logger.warning(
                        "[dingtalk] 下载地址获取失败 status=%s body=%s",
                        resp.status_code, resp.text[:200],
                    )
                    return None
                url = (resp.json() or {}).get("downloadUrl")
                if not url:
                    return None
                fr = await client.get(url)
            if fr.status_code == 200:
                return fr.content
            logger.warning("[dingtalk] 附件下载失败 status=%s", fr.status_code)
        except Exception:  # noqa: BLE001
            logger.exception("[dingtalk] 附件下载异常 code=%s", str(code)[:12])
        return None

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
