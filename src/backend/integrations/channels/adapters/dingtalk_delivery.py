"""dingtalk delivery behavior."""
from __future__ import annotations
import json
import logging
from typing import Any, Dict, Optional
import httpx
from integrations.channels.markdown import derive_title, downgrade_for_dingtalk
from integrations.channels.protocol import InboundMsg, SendResult, chunk_text
from integrations.channels.adapters.dingtalk_settings import DINGTALK_API_BASE
logger = logging.getLogger(__name__)

class DingTalkDelivery:
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
