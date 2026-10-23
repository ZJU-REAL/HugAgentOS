"""lark delivery behavior."""
from __future__ import annotations
import json
import logging
from typing import Any, Dict, Optional
import httpx
from integrations.channels.protocol import InboundMsg, SendResult
from integrations.channels.adapters.lark_settings import LARK_API_BASE
logger = logging.getLogger(__name__)

class LarkDelivery:
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
        from integrations.channels.protocol import chunk_text

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
