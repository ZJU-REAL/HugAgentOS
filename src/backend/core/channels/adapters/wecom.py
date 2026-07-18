"""WeCom (Enterprise WeChat) inbound channel adapter.

Self-built app + callback (webhook) mode. Isomorphic to [[lark]]'s Feishu webhook, differing in:
- **Encryption/decryption**: WeCom AES-256-CBC (``EncodingAESKey`` base64-decodes to a 32B key, IV=key[:16]),
  plaintext format ``random(16) + msg_len(4, big-endian) + msg + receiveid``. Same approach as ``lark._aes_decrypt``.
- **Signature verification**: ``msg_signature = sha1(sorted(token, timestamp, nonce, encrypt))`` (params in the query string).
- **URL verification**: when configuring the callback, WeCom sends a **GET** with ``echostr`` → return the decrypted plaintext (see the service's GET entry).
- **Reply**: a self-built app has no "edit message"; use **active message sending** ``/cgi-bin/message/send`` (touser+agentid).

Credential mapping (into config via create_bot's extra): corpid→app_id, secret→app_secret,
agent_id / token / aes_key → config. Session keying uses the sending user ``FromUserName`` (a self-built app is mostly a
user↔app 1:1 conversation). See internal design docs.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import struct
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, Optional

import httpx

from core.channels.protocol import ChannelCaps, InboundMsg, SendResult, chunk_text
from core.infra.crypto import decrypt_secret

logger = logging.getLogger(__name__)

WECOM_API_BASE = "https://qyapi.weixin.qq.com"


class WeComAdapter:
    caps = ChannelCaps(
        channel_type="wecom",
        max_message_len=2000,
        supports_markdown=False,
        splits_long_messages=False,
        supports_long_conn=False,  # uses webhook, reusing /v1/channels/{id}/webhook
        bind_mode="credentials",
        credential_fields=("app_id", "app_secret", "agent_id", "token", "aes_key"),
    )

    _token_cache: Dict[str, tuple] = {}

    # ── Credentials ─────────────────────────────────────────────────────
    @staticmethod
    def _cfg(conn: Any, key: str) -> str:
        cfg = conn.config if isinstance(conn.config, dict) else {}
        return decrypt_secret(cfg.get(f"{key}_enc")) or ""

    def _secret(self, conn: Any) -> str:
        return self._cfg(conn, "app_secret")

    def _agent_id(self, conn: Any) -> str:
        return self._cfg(conn, "agent_id")

    def _token(self, conn: Any) -> str:
        return self._cfg(conn, "token")

    def _aes_key(self, conn: Any) -> bytes:
        raw = self._cfg(conn, "aes_key")
        if not raw:
            return b""
        return base64.b64decode(raw + "=")  # EncodingAESKey is 43 chars → pad with '=' to decode 32 bytes

    async def _access_token(self, conn: Any) -> str:
        corpid, secret = conn.app_id, self._secret(conn)
        cached = self._token_cache.get(corpid)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        url = f"{WECOM_API_BASE}/cgi-bin/gettoken?corpid={corpid}&corpsecret={secret}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"企业微信 access_token 获取失败: {data}")
        self._token_cache[corpid] = (token, time.time() + int(data.get("expires_in", 7000)))
        return token

    async def validate_credentials(self, conn: Any) -> Dict[str, Any]:
        if not conn.app_id or not self._secret(conn):
            raise RuntimeError("缺少 CorpID / Secret")
        await self._access_token(conn)
        return {"app_id": conn.app_id}

    # ── AES-256-CBC decryption + signature ──────────────────────────────
    def _aes_decrypt(self, conn: Any, encrypt_b64: str) -> str:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        key = self._aes_key(conn)
        if not key:
            raise RuntimeError("缺少 EncodingAESKey")
        data = base64.b64decode(encrypt_b64)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        plain = decryptor.update(data) + decryptor.finalize()
        plain = plain[: -plain[-1]]  # strip PKCS7 padding
        # plaintext: random(16) + msg_len(4, big-endian) + msg + receiveid
        msg_len = struct.unpack(">I", plain[16:20])[0]
        return plain[20 : 20 + msg_len].decode("utf-8")

    @staticmethod
    def _signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
        items = sorted([token, timestamp, nonce, encrypt])
        return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()

    def verify_webhook(self, conn: Any, headers: Dict[str, str], body: bytes) -> bool:
        """msg_signature verification. Signature params are merged into headers by the routing layer (query string, lowercase keys)."""
        sig = headers.get("msg_signature") or ""
        ts = headers.get("timestamp") or ""
        nonce = headers.get("nonce") or ""
        encrypt = headers.get("_encrypt") or ""  # backfilled into headers by decrypt_webhook
        if not sig:
            return False
        return self._signature(self._token(conn), ts, nonce, encrypt) == sig

    def decrypt_webhook(self, conn: Any, body: bytes, headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """POST message body XML (containing <Encrypt>) → decrypt the inner XML → dict. Backfill encrypt into headers for signature verification."""
        try:
            root = ET.fromstring(body.decode("utf-8"))
            encrypt = (root.findtext("Encrypt") or "").strip()
        except Exception:  # noqa: BLE001
            return {}
        if headers is not None and encrypt:
            headers["_encrypt"] = encrypt
        if not encrypt:
            return {}
        try:
            inner = self._aes_decrypt(conn, encrypt)
            return self._xml_to_dict(inner)
        except Exception:  # noqa: BLE001
            logger.exception("[wecom] 消息解密失败 channel_id=%s", conn.channel_id)
            return {}

    def verify_url(self, conn: Any, params: Dict[str, str]) -> str:
        """GET URL verification: after checking msg_signature, decrypt echostr and return the plaintext (the routing layer writes it back as raw text)."""
        echostr = params.get("echostr") or ""
        sig = params.get("msg_signature") or ""
        ts = params.get("timestamp") or ""
        nonce = params.get("nonce") or ""
        if not echostr or self._signature(self._token(conn), ts, nonce, echostr) != sig:
            raise ValueError("企业微信 URL 校验签名不匹配")
        return self._aes_decrypt(conn, echostr)

    @staticmethod
    def _xml_to_dict(xml_text: str) -> Dict[str, Any]:
        try:
            root = ET.fromstring(xml_text)
        except Exception:  # noqa: BLE001
            return {}
        return {child.tag: (child.text or "") for child in root}

    # ── Event → InboundMsg ──────────────────────────────────────────────
    def parse_inbound(self, conn: Any, payload: Dict[str, Any]) -> Optional[InboundMsg]:
        msgtype = payload.get("MsgType")
        text = ""
        attachments = []
        if msgtype == "text":
            text = (payload.get("Content") or "").strip()
        elif msgtype == "image":
            media_id = payload.get("MediaId")
            if media_id:
                attachments.append({"kind": "image", "key": media_id, "name": f"{media_id}.jpg"})
        elif msgtype in ("file", "voice", "video"):
            media_id = payload.get("MediaId")
            if media_id:
                ext = {"file": "bin", "voice": "amr", "video": "mp4"}.get(msgtype, "bin")
                attachments.append({"kind": "file", "key": media_id, "name": f"{media_id}.{ext}"})
        elif msgtype == "event":
            return None  # follow / enter-app and similar events, ignore
        if not text and not attachments:
            return None
        from_user = payload.get("FromUserName") or ""
        chat_id = payload.get("ChatId") or ""
        chat_type = "group" if chat_id else "p2p"
        return InboundMsg(
            channel_id=conn.channel_id,
            channel_type="wecom",
            text=text,
            chat_type=chat_type,
            external_conversation_id=chat_id or from_user,
            sender_id=from_user,
            sender_name=from_user,
            message_id=payload.get("MsgId") or "",
            attachments=attachments,
            raw={"wecom_from_user": from_user, "wecom_chat_id": chat_id},
        )

    # ── Outbound push (active message sending) ──────────────────────────
    async def _send_app_message(self, conn: Any, inbound: InboundMsg, msgtype: str, body: Dict[str, Any]) -> SendResult:
        try:
            token = await self._access_token(conn)
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", str(exc))
        from_user = (inbound.raw or {}).get("wecom_from_user") or inbound.sender_id
        chat_id = (inbound.raw or {}).get("wecom_chat_id") or ""
        if chat_id:
            url = f"{WECOM_API_BASE}/cgi-bin/appchat/send?access_token={token}"
            payload = {"chatid": chat_id, "msgtype": msgtype, **body}
        else:
            url = f"{WECOM_API_BASE}/cgi-bin/message/send?access_token={token}"
            payload = {"touser": from_user, "agentid": self._agent_id(conn), "msgtype": msgtype, **body}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=payload)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", str(exc))
        if data.get("errcode", 0) == 0:
            return SendResult.ok()
        kind = "rate_limited" if data.get("errcode") in (45009, 45047) else "unknown"
        return SendResult.fail(kind, f"errcode={data.get('errcode')} {data.get('errmsg')}")

    async def send_text(self, conn: Any, inbound: InboundMsg, text: str) -> SendResult:
        return await self._send_app_message(conn, inbound, "text", {"text": {"content": text}})

    async def push(self, conn: Any, inbound: InboundMsg, content: str) -> SendResult:
        chunks = chunk_text(content, self.caps.max_message_len) or [content]
        first: Optional[SendResult] = None
        for i, c in enumerate(chunks):
            r = await self.send_text(conn, inbound, c)
            if i == 0:
                first = r
                if not r.success:
                    return r
        return first or SendResult.fail("unknown", "空内容")

    async def edit_message(self, conn: Any, message_id: str, text: str) -> SendResult:
        return SendResult.fail("bad_format", "企业微信不支持编辑消息")

    # ── File send/receive ───────────────────────────────────────────────
    async def download_resource(self, conn: Any, inbound: InboundMsg, attachment: Dict[str, Any]) -> Optional[bytes]:
        media_id = attachment.get("key")
        if not media_id:
            return None
        try:
            token = await self._access_token(conn)
            url = f"{WECOM_API_BASE}/cgi-bin/media/get?access_token={token}&media_id={media_id}"
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
            if resp.status_code == 200 and not resp.headers.get("content-type", "").startswith("application/json"):
                return resp.content
        except Exception:  # noqa: BLE001
            logger.exception("[wecom] 媒体下载失败 media_id=%s", media_id)
        return None

    async def push_file(self, conn: Any, inbound: InboundMsg, content: bytes, filename: str, mime_type: str) -> SendResult:
        try:
            token = await self._access_token(conn)
            mtype = "image" if (mime_type or "").startswith("image/") else "file"
            up_url = f"{WECOM_API_BASE}/cgi-bin/media/upload?access_token={token}&type={mtype}"
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(up_url, files={"media": (filename, content)})
            media_id = resp.json().get("media_id")
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", f"媒体上传失败: {exc}")
        if not media_id:
            return SendResult.fail("unknown", "未拿到 media_id")
        if mtype == "image":
            return await self._send_app_message(conn, inbound, "image", {"image": {"media_id": media_id}})
        return await self._send_app_message(conn, inbound, "file", {"file": {"media_id": media_id}})
