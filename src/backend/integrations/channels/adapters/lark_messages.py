"""lark messages behavior."""
from __future__ import annotations
import base64
import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Optional
import httpx
from integrations.channels.protocol import HistoryItem, InboundMsg
from integrations.channels.adapters.lark_settings import LARK_API_BASE
logger = logging.getLogger(__name__)
_MENTION_RE = re.compile(r"@_user_\d+")

class LarkMessages:
    def decrypt_webhook(self, conn: Any, body: bytes) -> Dict[str, Any]:
        """If an Encrypt Key is configured, the body looks like {"encrypt": "..."} and is AES-256-CBC decrypted; otherwise plain JSON."""
        try:
            raw = json.loads(body.decode("utf-8"))
        except Exception:
            return {}
        if "encrypt" not in raw:
            return raw
        key = self._encrypt_key(conn)
        if not key:
            logger.warning("[lark] 收到加密事件但未配 Encrypt Key")
            return {}
        return self._aes_decrypt(raw["encrypt"], key)

    @staticmethod
    def _aes_decrypt(encrypt_b64: str, encrypt_key: str) -> Dict[str, Any]:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
        data = base64.b64decode(encrypt_b64)
        iv, ct = data[:16], data[16:]
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plain = decryptor.update(ct) + decryptor.finalize()
        plain = plain[: -plain[-1]]  # strip PKCS7 padding
        return json.loads(plain.decode("utf-8"))

    def verify_webhook(self, conn: Any, headers: Dict[str, str], body: bytes) -> bool:
        """Feishu signature: sha256(timestamp + nonce + encrypt_key + body). Without an Encrypt Key, falls back to token verification."""
        key = self._encrypt_key(conn)
        sig = headers.get("x-lark-signature") or headers.get("X-Lark-Signature")
        ts = headers.get("x-lark-request-timestamp") or headers.get("X-Lark-Request-Timestamp")
        nonce = headers.get("x-lark-request-nonce") or headers.get("X-Lark-Request-Nonce")
        if key and sig and ts is not None and nonce is not None:
            h = hashlib.sha256((ts + nonce + key).encode("utf-8") + body).hexdigest()
            return h == sig
        # No encryption configured → fall back to verification_token (re-checked at the parse stage after decryption)
        return True

    def parse_inbound(self, conn: Any, payload: Dict[str, Any]) -> Optional[InboundMsg]:
        """Decrypted event dict → InboundMsg. Returns None for non-message events."""
        # schema 2.0: header.event_type; legacy schema: payload["event"]["type"]
        header = payload.get("header") or {}
        event_type = header.get("event_type") or payload.get("type")
        if event_type != "im.message.receive_v1":
            return None
        event = payload.get("event") or {}
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        mtype = message.get("message_type")
        text, attachments = self._extract_payload(mtype, message.get("content"))
        if not text and not attachments:
            return None  # unsupported message type (sticker/location, etc.)
        sender_open_id = (sender.get("sender_id") or {}).get("open_id", "")
        return self._make_inbound(
            conn=conn,
            chat_id=message.get("chat_id", ""),
            chat_type=message.get("chat_type", "p2p"),
            message_id=message.get("message_id", ""),
            text=text,
            sender_open_id=sender_open_id,
            sender_name=(sender.get("sender_id") or {}).get("user_id", "") or "",
            attachments=attachments,
            mentioned_ids=self._mentioned_ids(message),
            raw=payload,
        )

    @staticmethod
    def _mentioned_ids(message: Dict[str, Any]) -> List[str]:
        """open_ids of everyone @'d in this message (``message.mentions``).

        Only relevant once the app holds ``im:message.group_msg`` and Feishu starts pushing
        non-@ group messages too — until then every delivered group message has the bot in
        here anyway. The bot's own open_id is not known synchronously, so matching happens
        later in ``resolve_addressed``.
        """
        out: List[str] = []
        for m in message.get("mentions") or []:
            if not isinstance(m, dict):
                continue
            oid = (m.get("id") or {}).get("open_id") if isinstance(m.get("id"), dict) else None
            if oid:
                out.append(oid)
        return out

    @staticmethod
    def _strip_mentions(text: str) -> str:
        return _MENTION_RE.sub("", text or "").strip()

    @classmethod
    def _extract_text(cls, content: Any) -> str:
        if not content:
            return ""
        try:
            obj = json.loads(content) if isinstance(content, str) else content
            text = obj.get("text", "") if isinstance(obj, dict) else ""
        except Exception:
            text = str(content)
        return cls._strip_mentions(text)

    @classmethod
    def _extract_payload(cls, mtype: str, content: Any):
        """Extract (text, attachments) by message type.

        - text:  plain text body (@ mentions stripped)
        - image: image_key → attachment(kind=image)
        - file/media/audio: file_key + file_name → attachment(kind=file)
        - post (rich text): extract text + embedded image_key
        """
        try:
            obj = json.loads(content) if isinstance(content, str) else (content or {})
        except Exception:
            obj = {}
        if not isinstance(obj, dict):
            obj = {}
        attachments = []
        if mtype == "text":
            return cls._strip_mentions(obj.get("text", "")), attachments
        if mtype == "image":
            key = obj.get("image_key")
            if key:
                attachments.append({"kind": "image", "key": key, "name": f"{key}.png"})
            return "", attachments
        if mtype in ("file", "media", "audio"):
            key = obj.get("file_key")
            if key:
                name = obj.get("file_name") or f"{key}.bin"
                attachments.append({"kind": "file", "key": key, "name": name})
            return "", attachments
        if mtype == "post":  # rich text: extract text + embedded images
            texts, img_keys = [], []
            for node in cls._post_nodes(obj):
                if node.get("tag") == "text":
                    texts.append(node.get("text", ""))
                elif node.get("tag") == "img" and node.get("image_key"):
                    img_keys.append(node["image_key"])
            title = obj.get("title", "")
            body = " ".join(texts)
            text = (title + "\n" + body) if title else body
            attachments = [{"kind": "image", "key": k, "name": f"{k}.png"} for k in img_keys]
            return text.strip(), attachments
        return "", attachments

    @staticmethod
    def _post_nodes(obj: Dict[str, Any]):
        """Flatten all nodes of a post rich-text message (structure may be {content:[[node...]]} or wrapped in a language layer)."""
        content = obj.get("content") or []
        if isinstance(content, dict):  # language layer wrapper {"zh_cn": {"content": [...]}}
            content = next(iter(content.values()), {}).get("content", []) if content else []
        for line in content or []:
            for node in line or []:
                if isinstance(node, dict):
                    yield node

    @staticmethod
    def _make_inbound(
        *, conn: Any, chat_id: str, chat_type: str, message_id: str,
        text: str, sender_open_id: str, sender_name: str,
        raw: Dict[str, Any], attachments=None, mentioned_ids=None,
    ) -> InboundMsg:
        return InboundMsg(
            channel_id=conn.channel_id,
            channel_type="lark",
            text=text,
            chat_type=chat_type,
            external_conversation_id=chat_id,   # always keyed by chat_id
            sender_id=sender_open_id,
            sender_name=sender_name,
            message_id=message_id,
            attachments=attachments or [],
            # p2p is always aimed at the bot; in a group it depends on whether the bot's own
            # open_id is in the mention list — resolved asynchronously in resolve_addressed.
            addressed_to_bot=True if chat_type == "p2p" else None,
            mentioned_ids=list(mentioned_ids or []),
            raw={"lark_chat_id": chat_id, "lark_message_id": message_id},
        )

    async def fetch_history(
        self,
        conn: Any,
        conversation_id: str,
        *,
        since_ms: int,
        limit: int,
        newest_first: bool = False,
    ) -> List[HistoryItem]:
        """GET /open-apis/im/v1/messages — this chat's own messages, oldest-first.

        Uses the tenant token (app credentials), so it is a property of the bound app rather
        than of any user's session. Note this sits behind a *different* permission from event
        push: an app that is not allowed to receive non-@ group messages may still be allowed
        to read the chat's history, which is precisely why pulling is worth having.

        Feishu takes ``start_time`` in **seconds**; the caller works in milliseconds.
        Any failure returns [] — the caller treats "unsupported" and "failed" alike.
        """
        chat_id = (conversation_id or "").strip()
        if not chat_id:
            return []
        try:
            token = await self._tenant_token(conn.app_id, self._app_secret(conn))
            params = {
                "container_id_type": "chat",
                "container_id": chat_id,
                # Desc for the cold start so the first page holds the *newest* messages of
                # the window; Asc when walking forward from the cursor so nothing is skipped.
                "sort_type": "ByCreateTimeDesc" if newest_first else "ByCreateTimeAsc",
                "page_size": str(max(1, min(int(limit or 50), 50))),  # Feishu caps page_size at 50
            }
            if since_ms > 0:
                params["start_time"] = str(int(since_ms // 1000))
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{LARK_API_BASE}/open-apis/im/v1/messages",
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            body = resp.json() or {}
            if resp.status_code != 200 or body.get("code") not in (0, None):
                logger.warning(
                    "[lark] 群历史拉取失败 chat=%s code=%s msg=%s",
                    chat_id, body.get("code"), str(body.get("msg"))[:120],
                )
                return []
            return [
                item
                for item in (
                    self._history_item(raw) for raw in (body.get("data") or {}).get("items") or []
                )
                if item is not None
            ]
        except Exception:  # noqa: BLE001
            logger.warning("[lark] 群历史拉取异常 chat=%s", chat_id, exc_info=True)
            return []

    @classmethod
    def _history_item(cls, raw: Dict[str, Any]) -> Optional[HistoryItem]:
        """One ``im/v1/messages`` entry → HistoryItem, reusing the inbound content parser."""
        if not isinstance(raw, dict) or raw.get("deleted"):
            return None
        body = raw.get("body") or {}
        text, attachments = cls._extract_payload(raw.get("msg_type"), body.get("content"))
        if not text and not attachments:
            return None
        sender = raw.get("sender") or {}
        message_id = raw.get("message_id") or ""
        try:
            ts_ms = int(raw.get("create_time") or 0)
        except (TypeError, ValueError):
            ts_ms = 0
        return HistoryItem(
            message_id=message_id,
            # Feishu returns only ids here; a display name would need a directory lookup per
            # sender, which is not worth an extra API call per message for background context.
            sender_name=(sender.get("id") or "")[-6:] or "群成员",
            text=text,
            ts_ms=ts_ms,
            attachments=attachments,
            # download_resource resolves resources by message_id — carry it so a file pulled
            # from history stays fetchable later via channel_read_attachment.
            raw={"lark_message_id": message_id},
        )

    def _inbound_from_sdk_event(self, conn: Any, data: Any) -> Optional[InboundMsg]:
        """lark_oapi's typed P2ImMessageReceiveV1 event → InboundMsg."""
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        if message is None:
            return None
        mtype = getattr(message, "message_type", None)
        text, attachments = self._extract_payload(mtype, getattr(message, "content", None))
        if not text and not attachments:
            return None
        sender_id_obj = getattr(sender, "sender_id", None)
        open_id = getattr(sender_id_obj, "open_id", "") if sender_id_obj else ""
        return self._make_inbound(
            conn=conn,
            chat_id=getattr(message, "chat_id", "") or "",
            chat_type=getattr(message, "chat_type", "p2p") or "p2p",
            message_id=getattr(message, "message_id", "") or "",
            text=text,
            sender_open_id=open_id or "",
            sender_name="",
            attachments=attachments,
            mentioned_ids=self._sdk_mentioned_ids(message),
            raw={},
        )

    @staticmethod
    def _sdk_mentioned_ids(message: Any) -> List[str]:
        """SDK-typed counterpart of ``_mentioned_ids`` (``MentionEvent.id.open_id``).

        The long connection is the production path, so mention extraction must exist on both
        sides; missing it here would make every group message look un-@'d.
        """
        out: List[str] = []
        for m in getattr(message, "mentions", None) or []:
            ident = getattr(m, "id", None)
            oid = getattr(ident, "open_id", "") if ident is not None else ""
            if oid:
                out.append(oid)
        return out
