"""WeChat (WeixinClawBot / Tencent official iLink Bot API) inbound channel adapter.

Personal WeChat account bot — QR-scan binding (``bind_mode="qr"``), no credential form.
Underneath is Tencent's official iLink protocol (``ilinkai.weixin.qq.com``, HTTP/JSON):
- **Binding**: fetch QR code → poll scan status → obtain ``bot_token`` + ``baseurl`` (see
  channel_service's QR endpoints).
- **Inbound**: ``/ilink/bot/getupdates`` **long polling** (each call holds the connection ~35s)
  → normalized into InboundMsg. Long polling is a synchronous blocking call and runs directly on
  the manager's worker thread (no asyncio needed).
- **Outbound**: ``/ilink/bot/sendmessage``; you **must send back the inbound message's
  ``context_token``** (stored in raw), and the request body must be assembled exactly like the
  official client: ``message_type=2`` (BOT send; sending 1 gets silently dropped), ``client_id``
  (dedup, required), ``from_user_id=""``, ``base_info``; otherwise iLink returns HTTP 200 + an
  empty body ``{}`` but **does not deliver** (no error, nothing received) — this is exactly the
  root cause of "message received, agent replied, but nothing shows on the WeChat side".

Per-request headers: ``AuthorizationType: ilink_bot_token`` / ``Authorization: Bearer <bot_token>`` /
``X-WECHAT-UIN`` (base64 of a random uint32, anti-replay) / ``iLink-App-Id`` /
``iLink-App-ClientVersion`` (without the latter two, getupdates still receives but sendmessage
doesn't deliver). Constants are aligned with Tencent's official ``Tencent/openclaw-weixin``.
- **Outbound files** (``push_file``): media goes through the WeChat CDN + AES-128-ECB (PKCS7) —
  first ``getuploadurl`` for the presigned params (must include plaintext md5/size + ciphertext
  size + random aeskey), POST the ciphertext to the CDN (the response header
  ``x-encrypted-param`` is the download param), then ``sendmessage`` with ``file_item``/
  ``image_item`` (``media.aes_key`` is the **hex string then base64**, ``encrypt_type=1``, as the
  official client does).

⚠️ An iLink connection is valid for 24h and goes offline after 24h without new messages → a
re-scan is required. Unlike [[lark]]/[[dingtalk]], binding uses the QR device flow, and the
runtime long polling is driven by this adapter's ``_ILinkPoller``.
See internal design docs.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote

import httpx

from core.channels.protocol import ChannelCaps, InboundMsg, SendResult, chunk_text
from core.infra.crypto import decrypt_secret

logger = logging.getLogger(__name__)

ILINK_BASE = "https://ilinkai.weixin.qq.com"
# ── Protocol constants aligned with Tencent's official openclaw-weixin client ──
# If outbound sendmessage isn't assembled exactly like the official client (message_type=2 /
# client_id / base_info / the two App headers), iLink returns HTTP 200 + empty body {} but
# **silently drops it** — no error code, and the WeChat side receives nothing.
_CHANNEL_VERSION = "2.4.6"            # aligned with the official package version (base_info.channel_version + App-ClientVersion)
_ILINK_APP_ID = "bot"                # iLink-App-Id header (ilink_appid in the official package.json)
_ILINK_BOT_AGENT = "OpenClaw"        # base_info.bot_agent (official default)
_MSG_TYPE_TEXT = 1                   # item.type / inbound message_type: TEXT
_MSG_TYPE_BOT = 2                    # outbound message_type: BOT send (MessageType.BOT; sending 1 always gets dropped)
_MSG_STATE_FINISH = 2               # message_state: FINISH (complete message)
_ITEM_TYPE_IMAGE = 2                 # item.type: IMAGE (CDN-encrypted media)
_ITEM_TYPE_FILE = 4                  # item.type: FILE (CDN-encrypted attachment)
_UPLOAD_MEDIA_IMAGE = 1              # getuploadurl media_type: image
_UPLOAD_MEDIA_FILE = 3               # getuploadurl media_type: regular file
_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"  # concatenation fallback when the server gives no upload_full_url


def _client_version(ver: str) -> int:
    """iLink-App-ClientVersion: uint32 = (major<<16)|(minor<<8)|patch (e.g. 2.4.6 → 132102)."""
    parts = (ver.split(".") + ["0", "0", "0"])[:3]
    major, minor, patch = (int(p) if p.isdigit() else 0 for p in parts)
    return (major & 0xFF) << 16 | (minor & 0xFF) << 8 | (patch & 0xFF)


_ILINK_APP_CLIENT_VERSION = _client_version(_CHANNEL_VERSION)


def _uin_header() -> str:
    """X-WECHAT-UIN: random uint32 → decimal string → base64 (changes per request, anti-replay)."""
    return base64.b64encode(str(random.getrandbits(32)).encode()).decode()


def _client_id() -> str:
    """Unique client_id for sendmessage dedup (official format ``prefix:<millis>-<hex>``)."""
    return f"openclaw-weixin:{int(time.time() * 1000)}-{random.getrandbits(32):08x}"


def _aes_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-ECB + PKCS7 — the fixed encryption scheme for WeChat CDN media (aligned with the official client)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7

    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _render_qr_png_b64(text: str) -> str:
    """Render the to-be-scanned text/link into a QR code PNG → base64 (frontend renders it directly as data:image/png;base64)."""
    import io

    import qrcode

    buf = io.BytesIO()
    qrcode.make(text).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _bind_headers(bot_token: Optional[str] = None) -> Dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _uin_header(),
        "iLink-App-Id": _ILINK_APP_ID,
        "iLink-App-ClientVersion": str(_ILINK_APP_CLIENT_VERSION),
    }
    if bot_token:
        h["Authorization"] = f"Bearer {bot_token}"
    return h


class WeixinAdapter:
    caps = ChannelCaps(
        channel_type="weixin",
        max_message_len=2000,
        supports_markdown=False,
        splits_long_messages=False,
        supports_long_conn=True,
        bind_mode="qr",
        credential_fields=(),  # QR-scan binding, no credential form
    )

    # ── Credentials ─────────────────────────────────────────────────────
    @staticmethod
    def _bot_token(conn: Any) -> str:
        cfg = conn.config if isinstance(conn.config, dict) else {}
        return decrypt_secret(cfg.get("bot_token_enc")) or ""

    @staticmethod
    def _baseurl(conn: Any) -> str:
        cfg = conn.config if isinstance(conn.config, dict) else {}
        return (cfg.get("baseurl") or ILINK_BASE).rstrip("/")

    async def validate_credentials(self, conn: Any) -> Dict[str, Any]:
        token = self._bot_token(conn)
        if not token:
            raise RuntimeError("缺少 bot_token（请重新扫码绑定）")
        # Lightweight liveness check: fetch config once (typing_ticket). Failure isn't fatal, but an invalid token raises.
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self._baseurl(conn)}/ilink/bot/getconfig",
                    headers=_bind_headers(token), json={"base_info": {"channel_version": _CHANNEL_VERSION}},
                )
            if resp.status_code >= 400:
                raise RuntimeError(f"bot_token 校验失败 HTTP {resp.status_code}")
        except httpx.HTTPError as exc:
            raise RuntimeError(f"bot_token 校验失败: {exc}")
        return {"app_id": conn.app_id}

    def verify_webhook(self, conn: Any, headers: Dict[str, str], body: bytes) -> bool:
        return True  # long polling, no webhook

    # ── QR binding (no conn; called by the QR endpoints) ────────────────
    @staticmethod
    async def start_qr_bind() -> Dict[str, Any]:
        """Fetch the login QR code: returns {qrcode (string used for polling), qrcode_img_content (base64 PNG)}.

        ⚠️ In practice iLink's ``qrcode_img_content`` is the **to-be-scanned link**
        (``https://liteapp.weixin.qq.com/q/...``), not image bytes. The frontend renders it as a
        base64 PNG (``data:image/png;base64,``), so the link must be rendered here into an actual
        QR code PNG; if iLink switches back to returning base64 directly, pass it through as-is.
        """
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{ILINK_BASE}/ilink/bot/get_bot_qrcode",
                headers=_bind_headers(), params={"bot_type": 3},
            )
        data = resp.json()
        if not data.get("qrcode"):
            raise RuntimeError(f"获取微信登录二维码失败: {data}")
        raw_img = data.get("qrcode_img_content") or ""
        img_b64 = _render_qr_png_b64(raw_img) if raw_img.startswith("http") else raw_img
        return {"qrcode": data["qrcode"], "qrcode_img_content": img_b64}

    @staticmethod
    async def poll_qr_status(qrcode: str) -> Dict[str, Any]:
        """Poll the QR scan status: when confirmed, returns {status, bot_token, baseurl}.

        ⚠️ ``get_qrcode_status`` is server-side **long polling**: with no scan it hangs until the
        server times out (measured >20s). A client read timeout should be treated as "still
        waiting" rather than an error, otherwise channel_service.poll_weixin_bind catches it as a
        ``BadRequestError`` → HTTP 400, and the frontend misreads it as "binding failed" and stops polling.
        """
        try:
            async with httpx.AsyncClient(timeout=35) as client:
                resp = await client.get(
                    f"{ILINK_BASE}/ilink/bot/get_qrcode_status",
                    headers=_bind_headers(), params={"qrcode": qrcode},
                )
        except httpx.TimeoutException:
            return {"status": "waiting"}
        return resp.json() or {}

    # ── Event → InboundMsg ──────────────────────────────────────────────
    def parse_inbound(self, conn: Any, payload: Dict[str, Any]) -> Optional[InboundMsg]:
        """One iLink msg dict → InboundMsg. Non-text (image/file etc.) returns None for now."""
        if int(payload.get("message_type") or 0) != _MSG_TYPE_TEXT:
            return None
        text = ""
        for item in payload.get("item_list") or []:
            if isinstance(item, dict) and item.get("text_item"):
                text += item["text_item"].get("text", "")
        text = text.strip()
        if not text:
            return None
        from_user = payload.get("from_user_id") or ""
        return InboundMsg(
            channel_id=conn.channel_id,
            channel_type="weixin",
            text=text,
            chat_type="p2p",
            external_conversation_id=from_user,
            sender_id=from_user,
            sender_name=from_user.split("@")[0] if from_user else "",
            message_id=payload.get("context_token", "")[:64],  # iLink has no standalone msgId; dedupe via context_token
            attachments=[],
            raw={
                "weixin_to_user_id": from_user,        # reply to_user = inbound from_user
                "weixin_context_token": payload.get("context_token", ""),
            },
        )

    # ── Outbound push ───────────────────────────────────────────────────
    @staticmethod
    def _target(inbound: InboundMsg) -> tuple:
        """Outbound targeting: (to_user, context_token). Proactive delivery (synthetic msg) has no
        context_token; the iLink side lands it in the recipient's most recent active conversation, still deliverable."""
        raw = inbound.raw or {}
        to_user = raw.get("weixin_to_user_id") or inbound.external_conversation_id
        return to_user, raw.get("weixin_context_token") or ""

    async def _send_items(
        self, conn: Any, inbound: InboundMsg, item_list: List[Dict[str, Any]]
    ) -> SendResult:
        token = self._bot_token(conn)
        to_user, context_token = self._target(inbound)
        if not to_user:
            return SendResult.fail("bad_format", "缺少 to_user_id")
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user,
                "client_id": _client_id(),        # dedup ID, required by the official client
                "message_type": _MSG_TYPE_BOT,     # outbound must be 2 (BOT); sending 1 gets silently dropped by iLink
                "message_state": _MSG_STATE_FINISH,
                "context_token": context_token,
                "item_list": item_list,
            },
            "base_info": {"channel_version": _CHANNEL_VERSION, "bot_agent": _ILINK_BOT_AGENT},
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{self._baseurl(conn)}/ilink/bot/sendmessage",
                    headers=_bind_headers(token), json=body,
                )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", str(exc))
        if data.get("ret", 0) == 0:
            return SendResult.ok()
        return SendResult.fail("unknown", f"ret={data.get('ret')} {data.get('err_msg') or data}")

    async def _send(self, conn: Any, inbound: InboundMsg, text: str) -> SendResult:
        return await self._send_items(
            conn, inbound, [{"type": _MSG_TYPE_TEXT, "text_item": {"text": text}}]
        )

    async def send_text(self, conn: Any, inbound: InboundMsg, text: str) -> SendResult:
        return await self._send(conn, inbound, text)

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
        return SendResult.fail("bad_format", "微信不支持编辑消息")

    # ── Outbound file delivery (CDN AES-128-ECB encrypted upload → file_item/image_item) ──
    async def push_file(
        self, conn: Any, inbound: InboundMsg, content: bytes, filename: str, mime_type: str
    ) -> SendResult:
        token = self._bot_token(conn)
        if not token:
            return SendResult.fail("forbidden", "缺少 bot_token（请重新扫码绑定）")
        to_user, _ = self._target(inbound)
        if not to_user:
            return SendResult.fail("bad_format", "缺少 to_user_id")

        is_image = (mime_type or "").startswith("image/")
        rawsize = len(content)
        aeskey = os.urandom(16)
        filekey = os.urandom(16).hex()
        ciphertext = _aes_ecb_encrypt(content, aeskey)

        # 1. getuploadurl: presigned params must include plaintext md5/size + ciphertext size + aeskey (hex)
        upload_req = {
            "filekey": filekey,
            "media_type": _UPLOAD_MEDIA_IMAGE if is_image else _UPLOAD_MEDIA_FILE,
            "to_user_id": to_user,
            "rawsize": rawsize,
            "rawfilemd5": hashlib.md5(content, usedforsecurity=False).hexdigest(),
            "filesize": len(ciphertext),
            "no_need_thumb": True,
            "aeskey": aeskey.hex(),
            "base_info": {"channel_version": _CHANNEL_VERSION, "bot_agent": _ILINK_BOT_AGENT},
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self._baseurl(conn)}/ilink/bot/getuploadurl",
                    headers=_bind_headers(token), json=upload_req,
                )
            updata = resp.json()
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", f"getuploadurl 失败: {exc}")
        upload_url = (updata.get("upload_full_url") or "").strip()
        if not upload_url:
            upload_param = updata.get("upload_param") or ""
            if not upload_param:
                return SendResult.fail(
                    "unknown",
                    f"getuploadurl 未返回上传地址: ret={updata.get('ret')} {str(updata)[:200]}",
                )
            upload_url = f"{_CDN_BASE}/upload?encrypted_query_param={quote(upload_param)}&filekey={quote(filekey)}"

        # 2. POST the ciphertext to the CDN; the download param is in the x-encrypted-param response header
        try:
            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                cdn_resp = await client.post(
                    upload_url, content=ciphertext,
                    headers={"Content-Type": "application/octet-stream"},
                )
        except Exception as exc:  # noqa: BLE001
            return SendResult.fail("transient", f"CDN 上传失败: {exc}")
        download_param = cdn_resp.headers.get("x-encrypted-param")
        if cdn_resp.status_code != 200 or not download_param:
            err = cdn_resp.headers.get("x-error-message") or cdn_resp.text[:200]
            return SendResult.fail("unknown", f"CDN 上传失败 HTTP {cdn_resp.status_code}: {err}")

        # 3. sendmessage: media.aes_key is the hex string then base64 (as the official client does), encrypt_type=1
        media = {
            "encrypt_query_param": download_param,
            "aes_key": base64.b64encode(aeskey.hex().encode()).decode(),
            "encrypt_type": 1,
        }
        if is_image:
            item = {"type": _ITEM_TYPE_IMAGE, "image_item": {"media": media, "mid_size": len(ciphertext)}}
        else:
            item = {
                "type": _ITEM_TYPE_FILE,
                "file_item": {"media": media, "file_name": filename, "len": str(rawsize)},
            }
        return await self._send_items(conn, inbound, [item])

    # ── Long polling (runs on the manager worker thread, synchronous blocking) ──
    def make_ws_client(self, conn: Any, on_message: Callable[[InboundMsg], None]) -> Any:
        token = self._bot_token(conn)
        if not token:
            raise RuntimeError("缺少 bot_token，长轮询不可用")
        return _ILinkPoller(self, conn, token, self._baseurl(conn), on_message)


class _ILinkPoller:
    """iLink getupdates long poller: ``start()`` pulls messages in a blocking loop; ``stop()`` sets the flag to exit.

    A single getupdates call is held by the server for at most ~35s. On network/server errors,
    back off briefly and keep pulling (the manager's thread-level backoff is also a safety net).
    Each message is normalized and delivered to the main loop via ``on_message``.
    """

    def __init__(self, adapter: WeixinAdapter, conn: Any, bot_token: str, baseurl: str,
                 on_message: Callable[[InboundMsg], None]):
        self._adapter = adapter
        self._conn = conn
        self._token = bot_token
        self._baseurl = baseurl
        self._on_message = on_message
        self._stopped = threading.Event()
        self._buf = ""

    def start(self) -> None:
        with httpx.Client(timeout=45) as client:
            while not self._stopped.is_set():
                try:
                    resp = client.post(
                        f"{self._baseurl}/ilink/bot/getupdates",
                        headers=_bind_headers(self._token),
                        json={"get_updates_buf": self._buf,
                              "base_info": {"channel_version": _CHANNEL_VERSION}},
                    )
                    data = resp.json()
                except Exception as exc:  # noqa: BLE001
                    if self._stopped.is_set():
                        break
                    logger.debug("[weixin] getupdates 异常，2s 后重试: %s", exc)
                    self._stopped.wait(2.0)
                    continue
                self._buf = data.get("get_updates_buf", self._buf) or self._buf
                for msg in data.get("msgs") or []:
                    try:
                        inbound = self._adapter.parse_inbound(self._conn, msg)
                        if inbound is not None:
                            self._on_message(inbound)
                    except Exception:  # noqa: BLE001
                        logger.exception("[weixin] 长轮询消息处理失败 channel_id=%s", self._conn.channel_id)

    def stop(self) -> None:
        self._stopped.set()
