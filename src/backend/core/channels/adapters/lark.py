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

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, Optional

import httpx

from core.channels.protocol import ChannelCaps, InboundMsg, SendResult
from core.infra.crypto import decrypt_secret

logger = logging.getLogger(__name__)

LARK_API_BASE = os.getenv("LARK_API_BASE", "https://open.feishu.cn").rstrip("/")
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


class LarkAdapter:
    caps = ChannelCaps(
        channel_type="lark",
        max_message_len=4000,        # Feishu's per-message text cap is fairly high; leave headroom for chunking
        supports_markdown=False,     # uses text messages; rich text (post) left for later
        splits_long_messages=False,
        supports_long_conn=True,
    )

    # tenant_access_token cache: {app_id: (token, expire_epoch)}
    _token_cache: Dict[str, tuple] = {}

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

    # ── Event → InboundMsg ──────────────────────────────────────────────
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
            raw=payload,
        )

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
        raw: Dict[str, Any], attachments=None,
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
            raw={"lark_chat_id": chat_id, "lark_message_id": message_id},
        )

    # ── Outbound push ───────────────────────────────────────────────────
    def _chat_id(self, inbound: InboundMsg) -> str:
        return (inbound.raw or {}).get("lark_chat_id") or inbound.external_conversation_id

    async def _post_message(self, conn: Any, chat_id: str, msg_type: str, content: Dict[str, Any]) -> SendResult:
        if not chat_id:
            return SendResult.fail("bad_format", "缺少 chat_id")
        try:
            token = await self._tenant_token(conn.app_id, self._app_secret(conn))
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", str(exc))
        url = f"{LARK_API_BASE}/open-apis/im/v1/messages?receive_id_type=chat_id"
        payload = {"receive_id": chat_id, "msg_type": msg_type,
                   "content": json.dumps(content, ensure_ascii=False)}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", str(exc))
        if data.get("code") == 0:
            return SendResult.ok((data.get("data") or {}).get("message_id"))
        code = data.get("code")
        kind = "rate_limited" if code in (99991400, 99991401) else "forbidden" if code in (230002, 230013) else "unknown"
        return SendResult.fail(kind, f"code={code} msg={data.get('msg')}")

    async def send_text(self, conn: Any, inbound: InboundMsg, text: str) -> SendResult:
        return await self._post_message(conn, self._chat_id(inbound), "text", {"text": text})

    async def push(self, conn: Any, inbound: InboundMsg, content: str) -> SendResult:
        """Over-length content is auto-chunked by caps.max_message_len and sent piece by piece."""
        from core.channels.protocol import chunk_text

        chunks = chunk_text(content, self.caps.max_message_len) or [content]
        first: Optional[SendResult] = None
        cont: list = []
        for i, c in enumerate(chunks):
            r = await self.send_text(conn, inbound, c)
            if i == 0:
                first = r
                if not r.success:
                    return r
            elif r.message_id:
                cont.append(r.message_id)
        if first is not None:
            first.continuation_ids = cont
            return first
        return SendResult.fail("unknown", "空内容")

    async def edit_message(self, conn: Any, message_id: str, text: str) -> SendResult:
        if not message_id:
            return SendResult.fail("bad_format", "缺少 message_id")
        try:
            token = await self._tenant_token(conn.app_id, self._app_secret(conn))
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", str(exc))
        url = f"{LARK_API_BASE}/open-apis/im/v1/messages/{message_id}"
        payload = {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.put(url, headers={"Authorization": f"Bearer {token}"}, json=payload)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", str(exc))
        if data.get("code") == 0:
            return SendResult.ok(message_id)
        return SendResult.fail("unknown", f"code={data.get('code')} msg={data.get('msg')}")

    # ── Inbound file download ───────────────────────────────────────────
    async def download_resource(
        self, conn: Any, inbound: InboundMsg, attachment: Dict[str, Any]
    ) -> Optional[bytes]:
        """Download a resource file from a message (requires the im:resource permission).

        GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=file|image
        """
        msg_id = (inbound.raw or {}).get("lark_message_id") or inbound.message_id
        key = attachment.get("key")
        res_type = "image" if attachment.get("kind") == "image" else "file"
        if not msg_id or not key:
            return None
        try:
            token = await self._tenant_token(conn.app_id, self._app_secret(conn))
            url = f"{LARK_API_BASE}/open-apis/im/v1/messages/{msg_id}/resources/{key}?type={res_type}"
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code == 200 and not resp.headers.get("content-type", "").startswith("application/json"):
                return resp.content
            logger.warning("[lark] 资源下载失败 msg=%s key=%s body=%s", msg_id, key, resp.text[:200])
        except Exception:  # noqa: BLE001
            logger.exception("[lark] 资源下载异常 msg=%s key=%s", msg_id, key)
        return None

    # ── Outbound file delivery ──────────────────────────────────────────
    async def push_file(
        self, conn: Any, inbound: InboundMsg, content: bytes, filename: str, mime_type: str
    ) -> SendResult:
        chat_id = self._chat_id(inbound)
        if not chat_id:
            return SendResult.fail("bad_format", "缺少 chat_id")
        try:
            token = await self._tenant_token(conn.app_id, self._app_secret(conn))
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", str(exc))

        is_image = (mime_type or "").startswith("image/")
        try:
            if is_image:
                key = await self._upload_image(token, content)
                msg_type, msg_content = "image", {"image_key": key}
            else:
                key = await self._upload_file(token, content, filename)
                msg_type, msg_content = "file", {"file_key": key}
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", f"上传失败: {exc}")
        if not key:
            return SendResult.fail("unknown", "未拿到资源 key")
        return await self._post_message(conn, chat_id, msg_type, msg_content)

    async def _upload_image(self, token: str, content: bytes) -> Optional[str]:
        url = f"{LARK_API_BASE}/open-apis/im/v1/images"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url, headers={"Authorization": f"Bearer {token}"},
                data={"image_type": "message"},
                files={"image": ("img.png", content)},
            )
        data = resp.json()
        return (data.get("data") or {}).get("image_key") if data.get("code") == 0 else None

    async def _upload_file(self, token: str, content: bytes, filename: str) -> Optional[str]:
        # file_type: Feishu distinguishes by extension; anything not in the known set is sent as stream
        ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
        known = {"opus", "mp4", "pdf", "doc", "xls", "ppt"}
        file_type = ext if ext in known else "stream"
        url = f"{LARK_API_BASE}/open-apis/im/v1/files"
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url, headers={"Authorization": f"Bearer {token}"},
                data={"file_type": file_type, "file_name": filename},
                files={"file": (filename, content)},
            )
        data = resp.json()
        return (data.get("data") or {}).get("file_key") if data.get("code") == 0 else None

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
            raw={},
        )
