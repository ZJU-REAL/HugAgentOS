"""Slack inbound channel adapter.

A Slack app with a bot user, received over **Socket Mode** (long connection, no public
callback URL needed — the same shape as Lark/DingTalk long connections) or, optionally,
the Events API webhook (signed with the app's Signing Secret).

Credential mapping (create_bot's extra goes into config):
- ``app_id``      → Slack App ID (``A0…``, the bind lock; checked against the bot token)
- ``app_secret``  → Bot User OAuth Token (``xoxb-…``) for every Web API call
- ``app_token``   → App-Level Token (``xapp-…``, scope ``connections:write``) for Socket Mode
- ``signing_secret`` → only for webhook transport

Events: ``message`` (DMs = p2p; channels/private channels/group DMs = group) and
``app_mention``. A channel message and its app_mention share ``channel:ts`` as message_id,
so subscribing to both is deduplicated by inbound orchestration. Replies go into the
thread when the inbound message was in one. Sent message IDs are ``channel:ts`` because
``chat.update`` needs both.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from json import dumps as json_dumps
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

import httpx

from core.infra.crypto import decrypt_secret
from integrations.channels.adapters import slack_messages as sm
from integrations.channels.adapters.slack_settings import SLACK_API_BASE
from integrations.channels.protocol import (
    ChannelCaps,
    ChannelResourceError,
    HistoryItem,
    InboundMsg,
    SendResult,
    chunk_text,
)

logger = logging.getLogger(__name__)

_FORBIDDEN = {
    "not_in_channel", "channel_not_found", "is_archived", "missing_scope", "not_authed",
    "invalid_auth", "account_inactive", "token_revoked", "restricted_action", "cant_update_message",
}
_SIGNATURE_MAX_AGE = 300


def _error_kind(status: int, error: str) -> str:
    if status == 429 or error == "ratelimited":
        return "rate_limited"
    if error in _FORBIDDEN:
        return "forbidden"
    if error == "msg_too_long":
        return "too_long"
    if error in ("invalid_blocks", "no_text", "invalid_arguments"):
        return "bad_format"
    if status >= 500 or error in ("internal_error", "fatal_error", "service_unavailable"):
        return "transient"
    return "unknown"


def _split_message_id(message_id: str) -> tuple[str, str]:
    channel, _, ts = (message_id or "").partition(":")
    return channel, ts


class SlackAdapter:
    caps = ChannelCaps(
        channel_type="slack",
        # Slack truncates text past 40k chars but recommends staying under 4k per message.
        max_message_len=3900,
        supports_markdown=True,
        splits_long_messages=False,
        supports_long_conn=True,
        supports_group_observe=True,  # message.channels / message.groups deliver un-@'d messages
        bind_mode="credentials",
        credential_fields=("app_id", "app_secret", "app_token"),
    )

    # app_id → bot user id (stable for the app's lifetime; a restart re-fetches)
    _bot_user_cache: Dict[str, str] = {}

    # ── Credentials ─────────────────────────────────────────────────────
    @staticmethod
    def _cfg(conn: Any, key: str) -> str:
        cfg = conn.config if isinstance(conn.config, dict) else {}
        return decrypt_secret(cfg.get(f"{key}_enc")) or ""

    def _bot_token(self, conn: Any) -> str:
        return self._cfg(conn, "app_secret")

    async def _call(self, conn: Any, method: str, *, json: Optional[dict] = None,
                    data: Optional[dict] = None, params: Optional[dict] = None) -> Dict[str, Any]:
        """One Web API call with the bot token. Network errors raise; API errors are in the body."""
        headers = {"Authorization": f"Bearer {self._bot_token(conn)}"}
        async with httpx.AsyncClient(timeout=15) as client:
            if json is None and data is None:
                resp = await client.get(f"{SLACK_API_BASE}/{method}", params=params, headers=headers)
            else:
                resp = await client.post(f"{SLACK_API_BASE}/{method}", json=json, data=data, headers=headers)
        body = resp.json() if resp.content else {}
        body.setdefault("_status", resp.status_code)
        return body

    async def validate_credentials(self, conn: Any) -> Dict[str, Any]:
        token = self._bot_token(conn)
        if not conn.app_id or not token:
            raise RuntimeError("缺少 App ID / Bot Token")
        if not token.startswith("xoxb-"):
            raise RuntimeError("Bot Token 应以 xoxb- 开头（OAuth & Permissions → Bot User OAuth Token）")
        if conn.transport == "long_conn" and not self._cfg(conn, "app_token").startswith("xapp-"):
            raise RuntimeError("Socket Mode 需要以 xapp- 开头的 App-Level Token（Basic Information → App-Level Tokens）")
        auth = await self._call(conn, "auth.test", json={})
        if not auth.get("ok"):
            raise RuntimeError(f"Slack Bot Token 校验失败: {auth.get('error')}")
        bot_user = auth.get("user_id") or ""
        if bot_user:
            self._bot_user_cache[conn.app_id] = bot_user
        # The App ID is the bind lock, so make sure it really is this token's app. bots.info
        # needs users:read; without that scope the check is skipped rather than failing.
        if auth.get("bot_id"):
            info = await self._call(conn, "bots.info", params={"bot": auth["bot_id"]})
            app_id = ((info.get("bot") or {}).get("app_id") or "") if info.get("ok") else ""
            if app_id and app_id != conn.app_id:
                raise RuntimeError(f"App ID 与 Bot Token 不属于同一个应用（Token 属于 {app_id}）")
        return {"app_id": conn.app_id, "bot_user_id": bot_user, "team": auth.get("team") or ""}

    async def _bot_user_id(self, conn: Any) -> str:
        cached = self._bot_user_cache.get(conn.app_id)
        if cached:
            return cached
        auth = await self._call(conn, "auth.test", json={})
        bot_user = (auth.get("user_id") or "") if auth.get("ok") else ""
        if bot_user:
            self._bot_user_cache[conn.app_id] = bot_user
        return bot_user

    # ── Socket Mode ─────────────────────────────────────────────────────
    def make_ws_client(self, conn: Any, on_message: Callable[[InboundMsg], None]) -> Any:
        from integrations.channels.adapters.slack_socket import SlackSocketClient

        app_token = self._cfg(conn, "app_token")
        if not app_token:
            raise RuntimeError("缺少 App-Level Token，Socket Mode 不可用")
        return SlackSocketClient(self, conn, app_token, on_message)

    # ── Webhook (Events API) ────────────────────────────────────────────
    def verify_webhook(self, conn: Any, headers: Dict[str, str], body: bytes) -> bool:
        if conn.transport == "long_conn":
            return True
        secret = self._cfg(conn, "signing_secret")
        ts = headers.get("x-slack-request-timestamp") or ""
        sig = headers.get("x-slack-signature") or ""
        if not secret or not ts.isdigit() or abs(time.time() - int(ts)) > _SIGNATURE_MAX_AGE:
            return False
        base = b"v0:" + ts.encode() + b":" + (body or b"")
        expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    # ── Inbound ─────────────────────────────────────────────────────────
    def parse_inbound(self, conn: Any, payload: Dict[str, Any]) -> Optional[InboundMsg]:
        event = payload.get("event") if payload.get("type") == "event_callback" else payload
        if not isinstance(event, dict):
            return None
        etype = event.get("type")
        if etype not in ("message", "app_mention"):
            return None
        # Our own replies come back as events too; any bot-authored message is skipped so
        # two bots in one channel cannot talk to each other forever.
        if event.get("bot_id") or event.get("subtype") not in sm.CONTENT_SUBTYPES:
            return None
        user = event.get("user") or ""
        channel = event.get("channel") or ""
        ts = event.get("ts") or ""
        if not user or not channel or not ts:
            return None
        raw_text = event.get("text") or ""
        text = sm.inbound_text(raw_text)
        attachments = sm.file_attachments(event.get("files"))
        if not text and not attachments:
            return None
        is_dm = event.get("channel_type") == "im" or (etype == "message" and channel.startswith("D"))
        profile = event.get("user_profile") or {}
        sender_name = profile.get("display_name") or profile.get("real_name") or user
        return InboundMsg(
            channel_id=conn.channel_id,
            channel_type="slack",
            text=text,
            chat_type="p2p" if is_dm else "group",
            external_conversation_id=user if is_dm else channel,
            sender_id=user,
            sender_name=sender_name,
            message_id=f"{channel}:{ts}",
            addressed_to_bot=True if (is_dm or etype == "app_mention") else None,
            mentioned_ids=[] if is_dm else sm.mentioned_ids(raw_text),
            attachments=attachments,
            # A reply inside a thread names its root; send_text returns IDs as "channel:ts".
            reply_to_message_id=(f"{channel}:{event['thread_ts']}"
                                 if event.get("thread_ts") and event.get("thread_ts") != ts else ""),
            raw={"slack_channel": channel, "slack_thread_ts": event.get("thread_ts") or ""},
        )

    async def resolve_addressed(self, conn: Any, inbound: InboundMsg) -> bool:
        """True when the bot user is @'d. Fails open when the bot identity is unknown."""
        try:
            bot_user = await self._bot_user_id(conn)
        except Exception:  # noqa: BLE001
            logger.debug("[slack] 机器人 user_id 获取失败，按「已被 @」处理", exc_info=True)
            return True
        return not bot_user or bot_user in (inbound.mentioned_ids or [])

    # ── Outbound ────────────────────────────────────────────────────────
    @staticmethod
    def _target(inbound: InboundMsg) -> tuple[str, str]:
        raw = inbound.raw or {}
        return raw.get("slack_channel") or inbound.external_conversation_id, raw.get("slack_thread_ts") or ""

    @staticmethod
    def _result(body: Dict[str, Any]) -> Optional[SendResult]:
        if body.get("ok"):
            return None
        error = str(body.get("error") or "")
        return SendResult.fail(_error_kind(int(body.get("_status") or 200), error), error)

    def prepare_markdown(self, text: str) -> str:
        return sm.markdown_to_mrkdwn(text)

    async def send_text(self, conn: Any, inbound: InboundMsg, text: str) -> SendResult:
        channel, thread_ts = self._target(inbound)
        payload: Dict[str, Any] = {"channel": channel, "text": text, "mrkdwn": True}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        try:
            body = await self._call(conn, "chat.postMessage", json=payload)
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", str(exc))
        failed = self._result(body)
        if failed is not None:
            return failed
        return SendResult.ok(f"{body.get('channel') or channel}:{body.get('ts')}")

    # Text reaching send_markdown has already been through prepare_markdown.
    send_markdown = send_text

    async def push(self, conn: Any, inbound: InboundMsg, content: str) -> SendResult:
        chunks = chunk_text(self.prepare_markdown(content), self.caps.max_message_len)
        first: Optional[SendResult] = None
        for chunk in chunks:
            r = await self.send_text(conn, inbound, chunk)
            if first is None:
                first = r
                if not r.success:
                    return r
            elif r.success and r.message_id:
                first.continuation_ids.append(r.message_id)
        return first or SendResult.fail("unknown", "空内容")

    async def edit_message(self, conn: Any, message_id: str, text: str) -> SendResult:
        channel, ts = _split_message_id(message_id)
        if not channel or not ts:
            return SendResult.fail("bad_format", "无效的 Slack 消息 ID")
        try:
            body = await self._call(conn, "chat.update", json={"channel": channel, "ts": ts, "text": text})
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", str(exc))
        return self._result(body) or SendResult.ok(message_id)

    async def recall_message(self, conn: Any, inbound: InboundMsg, message_id: str) -> SendResult:
        channel, ts = _split_message_id(message_id)
        try:
            body = await self._call(conn, "chat.delete", json={"channel": channel, "ts": ts})
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", str(exc))
        return self._result(body) or SendResult.ok(message_id)

    # ── Group history ───────────────────────────────────────────────────
    async def fetch_history(self, conn: Any, conversation_id: str, *, since_ms: int, limit: int,
                            newest_first: bool = False) -> List[HistoryItem]:
        """conversations.history (newest-first from Slack; the caller sorts). Failures → []."""
        channel = (conversation_id or "").strip()
        if not channel:
            return []
        params: Dict[str, Any] = {"channel": channel, "limit": max(1, min(int(limit or 50), 200))}
        if since_ms > 0:
            params["oldest"] = f"{since_ms / 1000:.6f}"
            params["inclusive"] = "false"  # keep the cursor message itself out
        try:
            body = await self._call(conn, "conversations.history", params=params)
        except Exception:  # noqa: BLE001
            logger.warning("[slack] 历史拉取异常 channel=%s", channel, exc_info=True)
            return []
        if not body.get("ok"):
            logger.warning("[slack] 历史拉取失败 channel=%s error=%s", channel, body.get("error"))
            return []
        items = []
        for msg in body.get("messages") or []:
            if msg.get("bot_id") or msg.get("subtype") not in sm.CONTENT_SUBTYPES:
                continue
            ts = str(msg.get("ts") or "")
            text = sm.inbound_text(msg.get("text") or "")
            files = sm.file_attachments(msg.get("files"))
            if not ts or (not text and not files):
                continue
            items.append(HistoryItem(
                message_id=f"{channel}:{ts}",
                sender_name=(msg.get("user_profile") or {}).get("display_name") or msg.get("user") or "",
                text=text,
                ts_ms=int(float(ts) * 1000),
                attachments=files,
                raw={"slack_channel": channel},
            ))
        if not newest_first:
            items.sort(key=lambda i: i.ts_ms)
        return items[: max(1, int(limit or 50))]

    # ── Files ───────────────────────────────────────────────────────────
    @staticmethod
    def _trusted_file_url(url: str) -> bool:
        """Only send the bot token to Slack's own file host (or the configured API origin)."""
        parts = urlsplit(url or "")
        api = urlsplit(SLACK_API_BASE)
        return (parts.scheme == "https" and parts.hostname == "files.slack.com") or (
            parts.scheme == api.scheme and parts.netloc == api.netloc
        )

    async def download_resource(self, conn: Any, inbound: InboundMsg,
                                attachment: Dict[str, Any]) -> Optional[bytes]:
        url = attachment.get("key") or ""
        if not self._trusted_file_url(url):
            logger.warning("[slack] 拒绝下载非 Slack 文件地址 %s", url[:120])
            return None
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {self._bot_token(conn)}"})
        except Exception:  # noqa: BLE001
            logger.exception("[slack] 文件下载失败")
            return None
        if resp.status_code != 200:
            return None
        # Without files:read Slack answers with its HTML sign-in page instead of an error.
        if resp.headers.get("content-type", "").startswith("text/html"):
            raise ChannelResourceError("Slack 应用缺少 files:read 权限，无法读取附件；请在 OAuth & Permissions 中添加后重新安装应用")
        return resp.content

    async def push_file(self, conn: Any, inbound: InboundMsg, content: bytes, filename: str,
                        mime_type: str) -> SendResult:
        channel, thread_ts = self._target(inbound)
        try:
            ticket = await self._call(conn, "files.getUploadURLExternal",
                                      data={"filename": filename, "length": str(len(content))})
            failed = self._result(ticket)
            if failed is not None:
                return failed
            async with httpx.AsyncClient(timeout=120) as client:
                up = await client.post(ticket["upload_url"], content=content,
                                       headers={"Content-Type": mime_type or "application/octet-stream"})
            if up.status_code != 200:
                return SendResult.fail("transient", f"文件上传失败 HTTP {up.status_code}")
            form = {"files": json_dumps([{"id": ticket["file_id"], "title": filename}]),
                    "channel_id": channel}
            if thread_ts:
                form["thread_ts"] = thread_ts
            done = await self._call(conn, "files.completeUploadExternal", data=form)
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", f"文件发送失败: {exc}")
        return self._result(done) or SendResult.ok(ticket["file_id"])
