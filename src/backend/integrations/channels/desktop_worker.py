"""Desktop worker: authorized cloud channel I/O with the existing local chat runtime."""

from __future__ import annotations
from features.desktop.bridge import identity as bridge_identity

import asyncio
import hashlib
import logging
import platform

import httpx
from integrations.channels.protocol import ChannelCaps, HistoryItem, InboundMsg, SendResult
from integrations.channels.models import ChannelConnection
from core.db.models import ContentBlock, UserShadow
from core.db.threads import run_db, run_in_session
from core.infra.exceptions import AccessDeniedError, BadRequestError
from features.desktop.auth import bridge as bridge_auth
from integrations.channels.relay_rpc import pack_bytes, unpack_bytes
from sqlalchemy import select

logger = logging.getLogger(__name__)
GRANT_PREFIX = "channel_desktop_grant:"
RECEIPT_PREFIX = "channel_delivery:"


async def cloud_post(captured, path, payload):
    bridge_identity.require_current_account(captured)
    current = bridge_identity.get_state()
    if not current:
        raise AccessDeniedError("桌面云端会话已失效")
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            response = await client.post(
                current["cloud_base"] + "/api/v1/channels/desktop/" + path,
                headers=bridge_identity.cloud_headers(current),
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise BadRequestError("渠道执行服务连接失败，请检查网络后重试") from exc
    bridge_identity.require_current_account(captured)
    try:
        data = response.json()
    except ValueError as exc:
        raise BadRequestError("渠道执行服务响应格式无效") from exc
    if not isinstance(data, dict):
        raise BadRequestError("渠道执行服务响应格式无效")
    if response.status_code in (401, 403):
        raise AccessDeniedError(str(data.get("message") or "渠道授权已失效，请重新登录"))
    # All channel routes use core.infra.responses, including registration and replies.
    if not response.is_success or data.get("code") not in (10000, 10001):
        raise BadRequestError(str(data.get("message") or "渠道执行服务不可用"))
    if "data" not in data:
        raise BadRequestError("渠道执行服务响应格式无效")
    return data["data"]


def identity_owner(db):
    identity = bridge_identity.get_identity_state()
    if not bridge_auth.bridge_enabled() or not bridge_identity.get_state() or not identity:
        raise AccessDeniedError("请在已登录的混合模式桌面端使用本机机器人")
    owner = db.scalar(
        select(UserShadow.user_id).where(
            UserShadow.user_center_id == identity["shell_user_center_id"]
        )
    )
    if not owner:
        raise AccessDeniedError("本机身份尚未就绪")
    return identity, owner


def _store_grant(db, captured, grant, identity, owner):
    with bridge_identity.account_scope(captured):
        db.add(
            ContentBlock(
                id=GRANT_PREFIX + grant["binding_id"], payload={**identity, "local_owner": owner}
            )
        )
        db.commit()


async def prepare_binding(owner):
    identity, actual_owner = await run_in_session(identity_owner)
    if owner != actual_owner:
        raise AccessDeniedError("本机账号不匹配")
    captured = bridge_identity.get_state()
    grant = await cloud_post(captured, "register", {"device_name": platform.node() or "本机"})
    if (
        not isinstance(grant, dict)
        or not isinstance(grant.get("binding_id"), str)
        or not grant["binding_id"]
        or grant.get("device_id") != identity["device_id"]
        or not isinstance(grant.get("device_name"), str)
    ):
        raise BadRequestError("本机授权响应格式无效，请重试")
    await run_in_session(_store_grant, captured, grant, identity, owner)
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
            from core.harness.registry.run_cancellation import cancel_run

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


def _accept_delivery(factory, identity, owner, item, receipt_id):
    from core.capabilities.agents.edition import UserAgentService

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
    return local_id


def _finish_receipt(factory, receipt_id, binding_id, error):
    with factory() as db:
        receipt = db.get(ContentBlock, receipt_id)
        receipt.payload = {"state": "failed" if error else "completed", "binding_id": binding_id}
        db.commit()


async def run_delivery(factory, captured, identity, owner, item):
    from integrations.channels.inbound import handle_inbound
    from integrations.channels.registry import scoped_adapter

    bridge_identity.require_current_account(captured)
    receipt_id = RECEIPT_PREFIX + item["delivery_id"]
    local_id = await run_db(_accept_delivery, factory, identity, owner, item, receipt_id)
    msg = InboundMsg(**{**item["message"], "channel_id": local_id})
    adapter = DesktopChannelAdapter(captured, item, local_id)
    with scoped_adapter(adapter):
        task = asyncio.create_task(handle_inbound(msg))
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=8)
                bridge_identity.require_current_account(captured)
                await cloud_post(captured, item["delivery_id"] + "/renew", {"lease": item["lease"]})
                if done:
                    break
            await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await adapter.cancel()
    await run_db(_finish_receipt, factory, receipt_id, item["binding_id"], adapter.error)
    return adapter.error


def _claim_inputs(factory):
    with factory() as db:
        identity, owner = identity_owner(db)
        return identity, owner, local_grants(db, identity, owner)


def _forget_grants(factory, captured, binding_ids):
    with bridge_identity.account_scope(captured), factory() as db:
        for binding_id in binding_ids:
            row = db.get(ContentBlock, GRANT_PREFIX + binding_id)
            if row:
                db.delete(row)
        db.commit()


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
                identity, owner, bindings = await run_db(_claim_inputs, self.factory)
                captured = bridge_identity.get_state()
                for offset in range(0, len(bindings), 200):
                    batch = bindings[offset : offset + 200]
                    reply = await cloud_post(captured, "claim", {"binding_ids": batch})
                    retained = set(reply["retained_binding_ids"])
                    await run_db(_forget_grants, self.factory, captured, set(batch) - retained)
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
