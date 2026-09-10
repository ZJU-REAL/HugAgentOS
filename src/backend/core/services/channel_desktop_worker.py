"""Desktop worker: authorized cloud channel I/O with the existing local chat runtime."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import platform

import httpx
from core.channels.protocol import ChannelCaps, HistoryItem, InboundMsg, SendResult
from core.db.models import ChannelConnection, ContentBlock, UserShadow
from core.infra.exceptions import AccessDeniedError, BadRequestError
from core.services import desktop_cloud_bridge as bridge
from core.services.channel_relay_rpc import pack_bytes, unpack_bytes
from sqlalchemy import select

logger = logging.getLogger(__name__)
GRANT_PREFIX = "channel_desktop_grant:"
RECEIPT_PREFIX = "channel_delivery:"


async def cloud_post(captured, path, payload):
    bridge.require_current_account(captured)
    current = bridge.get_state()
    if not current:
        raise AccessDeniedError("桌面云端会话已失效")
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post(
            current["cloud_base"] + "/api/v1/channels/desktop/" + path,
            headers=bridge.cloud_headers(current),
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    bridge.require_current_account(captured)
    if data.get("code") not in (0, 200, 201):
        raise BadRequestError(str(data.get("message") or "渠道执行服务不可用"))
    return data.get("data")


def identity_owner(db):
    identity = bridge.get_identity_state()
    if not bridge.bridge_enabled() or not bridge.get_state() or not identity:
        raise AccessDeniedError("请在已登录的混合模式桌面端使用本机机器人")
    owner = db.scalar(
        select(UserShadow.user_id).where(
            UserShadow.user_center_id == identity["shell_user_center_id"]
        )
    )
    if not owner:
        raise AccessDeniedError("本机身份尚未就绪")
    return identity, owner


async def prepare_binding(db, owner):
    identity, actual_owner = identity_owner(db)
    if owner != actual_owner:
        raise AccessDeniedError("本机账号不匹配")
    captured = bridge.get_state()
    grant = await cloud_post(captured, "register", {"device_name": platform.node() or "本机"})
    with bridge.account_scope(captured):
        db.add(
            ContentBlock(
                id=GRANT_PREFIX + grant["binding_id"], payload={**identity, "local_owner": owner}
            )
        )
        db.commit()
    return grant


def local_grants(db, identity, owner):
    rows = db.scalars(select(ContentBlock).where(ContentBlock.id.like(GRANT_PREFIX + "%"))).all()
    return [
        row.id[len(GRANT_PREFIX) :]
        for row in rows
        if (row.payload or {}).get("local_owner") == owner
        and all(
            (row.payload or {}).get(key) == identity[key]
            for key in ("cloud_base", "subject", "device_id")
        )
    ]


class DesktopChannelAdapter:
    """Adapter interface scoped to exactly one claimed cloud delivery."""

    def __init__(self, captured, item, local_channel_id):
        self.captured, self.item = captured, item
        self.local_channel_id = local_channel_id
        self.caps = ChannelCaps(**item["caps"])
        self.sequence = 0
        self.run_id = None
        self.owner_id = None
        self.error = None

    def check_connection(self, conn):
        if conn.channel_id != self.local_channel_id:
            raise AccessDeniedError("渠道操作不属于当前机器人")

    def record_run(self, run_id, owner_id):
        self.run_id, self.owner_id = run_id, owner_id

    async def cancel(self):
        if self.run_id and self.owner_id:
            from orchestration.chat_run_executor import cancel_run

            await cancel_run(self.run_id, user_id=self.owner_id)

    async def call(self, method, args):
        self.sequence += 1
        result = await cloud_post(
            self.captured,
            self.item["delivery_id"] + "/operation",
            {
                "lease": self.item["lease"],
                "operation_id": str(self.sequence),
                "method": method,
                "args": args,
            },
        )
        return result

    async def resolve_addressed(self, conn, msg):
        self.check_connection(conn)
        return await self.call("resolve_addressed", {})

    async def fetch_history(self, conn, conversation_id, **kwargs):
        self.check_connection(conn)
        return [HistoryItem(**item) for item in await self.call("fetch_history", kwargs)]

    async def download_resource(self, conn, msg, attachment):
        self.check_connection(conn)
        result = await self.call("download_resource", {"attachment": attachment})
        return unpack_bytes(result) if result is not None else None

    async def push_file(self, conn, msg, content, filename, mime_type):
        self.check_connection(conn)
        result = SendResult(
            **await self.call(
                "push_file",
                {"content": pack_bytes(content), "filename": filename, "mime_type": mime_type},
            )
        )
        if not result.success:
            self.error = "渠道文件回传失败"
        return result

    async def send_text(self, conn, msg, text):
        self.check_connection(conn)
        result = SendResult(**await self.call("send_text", {"text": text}))
        if not result.success:
            self.error = "渠道回复发送失败"
        return result

    async def send_placeholder(self, conn, msg, text):
        self.check_connection(conn)
        return SendResult(**await self.call("send_placeholder", {"text": text}))

    async def send_markdown(self, conn, msg, text):
        self.check_connection(conn)
        result = SendResult(**await self.call("send_markdown", {"text": text}))
        if not result.success:
            self.error = "渠道回复发送失败"
        return result

    async def edit_message(self, conn, message_id, text):
        self.check_connection(conn)
        return SendResult(
            **await self.call("edit_message", {"message_id": message_id, "text": text})
        )

    async def recall_message(self, conn, msg, message_id):
        self.check_connection(conn)
        return SendResult(**await self.call("recall_message", {"message_id": message_id}))


async def run_delivery(factory, captured, identity, owner, item):
    from core.channels.inbound import handle_inbound
    from core.channels.registry import scoped_adapter
    from core.services.user_agent_service import UserAgentService

    bridge.require_current_account(captured)
    receipt_id = RECEIPT_PREFIX + item["delivery_id"]
    with factory() as db:
        if item["binding_id"] not in local_grants(db, identity, owner):
            raise AccessDeniedError("机器人尚未在这台电脑授权")
        if db.get(ContentBlock, receipt_id):
            raise BadRequestError("本机已接收过此消息，不重复执行")
        bot = item["bot"]
        if bot.get("agent_id"):
            UserAgentService(db).get_raw_by_id(bot["agent_id"], user_id=owner)
        # Binding identity isolates default-workspace chat history across accounts/clouds.
        local_id = (
            "dchan_"
            + hashlib.sha256(
                (identity["cloud_base"] + identity["subject"] + item["binding_id"]).encode()
            ).hexdigest()[:40]
        )
        conn = db.get(ChannelConnection, local_id)
        config = {"desktop_relay": True, "desktop_relay_agent_id": bot.get("agent_id")}
        if not conn:
            conn = ChannelConnection(
                channel_id=local_id,
                owner_user_id=owner,
                channel_type=bot["channel_type"],
                app_id=local_id,
                transport="webhook",
                config=config,
                enabled=True,
                status="connected",
            )
            db.add(conn)
        if conn.owner_user_id != owner:
            raise AccessDeniedError("本机机器人账号不匹配")
        conn.display_name = bot["display_name"]
        conn.config = config
        conn.group_listen_mode = bot["group_listen_mode"]
        conn.resource_scope = bot.get("resource_scope")
        db.add(
            ContentBlock(
                id=receipt_id, payload={"state": "accepted", "binding_id": item["binding_id"]}
            )
        )
        db.commit()
    msg = InboundMsg(**{**item["message"], "channel_id": local_id})
    adapter = DesktopChannelAdapter(captured, item, local_id)
    with scoped_adapter(adapter):
        task = asyncio.create_task(handle_inbound(msg))
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=8)
                bridge.require_current_account(captured)
                await cloud_post(captured, item["delivery_id"] + "/renew", {"lease": item["lease"]})
                if done:
                    break
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await adapter.cancel()
    with factory() as db:
        receipt = db.get(ContentBlock, receipt_id)
        receipt.payload = {
            "state": "failed" if adapter.error else "completed",
            "binding_id": item["binding_id"],
        }
        db.commit()
    return adapter.error


class ChannelDesktopWorker:
    def __init__(self, factory):
        self.factory = factory
        self.task = None

    def start(self):
        self.task = asyncio.create_task(self.loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def loop(self):
        while True:
            captured = None
            item = None
            try:
                with self.factory() as db:
                    identity, owner = identity_owner(db)
                    bindings = local_grants(db, identity, owner)
                captured = bridge.get_state()
                for offset in range(0, len(bindings), 200):
                    batch = bindings[offset : offset + 200]
                    reply = await cloud_post(captured, "claim", {"binding_ids": batch})
                    retained = set(reply["retained_binding_ids"])
                    with bridge.account_scope(captured), self.factory() as db:
                        for binding_id in set(batch) - retained:
                            row = db.get(ContentBlock, GRANT_PREFIX + binding_id)
                            if row:
                                db.delete(row)
                        db.commit()
                    item = reply["delivery"]
                    if item:
                        error = await run_delivery(self.factory, captured, identity, owner, item)
                        await cloud_post(
                            captured,
                            item["delivery_id"] + "/complete",
                            {"lease": item["lease"], "error": error},
                        )
                        break
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never log tokens, channel messages or file contents. No replay after a
                # claim: cloud expiry makes ambiguous execution visible as a failure.
                if item and captured:
                    try:
                        await cloud_post(
                            captured,
                            item["delivery_id"] + "/complete",
                            {
                                "lease": item["lease"],
                                "error": "本机执行中断，请检查桌面端后重发消息",
                            },
                        )
                    except Exception:
                        pass
                logger.debug("desktop channel worker unavailable", exc_info=False)
            await asyncio.sleep(3)
