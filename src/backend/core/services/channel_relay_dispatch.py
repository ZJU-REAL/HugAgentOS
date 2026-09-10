"""Cloud ingress routing and visible expiry for interrupted desktop deliveries."""

import asyncio
import logging
import time

from core.auth.desktop_bridge import bridge_enabled
from core.channels.protocol import InboundMsg
from core.channels.registry import get_adapter
from core.db.engine import SessionLocal
from core.db.models import ChannelConnection
from core.db.models.channel_relay import ChannelRelayDelivery, ChannelRelayOperation
from core.services.channel_relay import ChannelRelayService, execution
from sqlalchemy import delete, select, update

logger = logging.getLogger(__name__)


async def dispatch_to_desktop(msg, factory):
    if bridge_enabled():
        return False
    with factory() as db:
        conn = db.get(ChannelConnection, msg.channel_id)
        if not conn or not execution(conn):
            return False
        if not conn.enabled:
            return True
        from core.channels.inbound import _resolve_addressed

        adapter = get_adapter(conn.channel_type)
        if msg.chat_type == "group":
            msg.addressed_to_bot = await _resolve_addressed(adapter, conn, msg)
            if not msg.addressed_to_bot and conn.group_listen_mode != "observe_all":
                return True
        try:
            ChannelRelayService(db).enqueue(conn, msg)
        except Exception:
            db.rollback()
            if msg.addressed_to_bot is not False:
                await adapter.send_text(
                    conn, msg, "本机机器人暂不可用，请确认电脑在线、桌面端已登录后重发消息。"
                )
        return True


async def expire_deliveries(factory):
    with factory() as db:
        expired = db.scalars(
            select(ChannelRelayDelivery)
            .where(
                ChannelRelayDelivery.status.in_(["pending", "running"]),
                ChannelRelayDelivery.deadline < time.time(),
            )
            .limit(30)
        ).all()
        for item in expired:
            changed = db.execute(
                update(ChannelRelayDelivery)
                .where(
                    ChannelRelayDelivery.delivery_id == item.delivery_id,
                    ChannelRelayDelivery.status.in_(["pending", "running"]),
                    ChannelRelayDelivery.deadline < time.time(),
                )
                .values(status="failed", error="设备未及时完成任务，未自动重试")
            )
            db.commit()
            if changed.rowcount != 1:
                continue
            conn = db.get(ChannelConnection, item.channel_id)
            msg = InboundMsg(**item.payload["message"])
            if conn and conn.enabled and msg.addressed_to_bot is not False:
                try:
                    await get_adapter(conn.channel_type).send_text(
                        conn,
                        msg,
                        "本机执行已超时或中断，结果可能尚未发送。请检查桌面端执行记录后再决定是否重发，系统不会自动重复执行。",
                    )
                except Exception:
                    logger.warning("desktop channel expiry notification failed")
        old = db.scalars(
            select(ChannelRelayDelivery)
            .where(
                ChannelRelayDelivery.created_at < time.time() - 7 * 86400,
                ChannelRelayDelivery.status.in_(["done", "failed", "cancelled"]),
            )
            .limit(30)
        ).all()
        for item in old:
            db.execute(
                delete(ChannelRelayOperation).where(
                    ChannelRelayOperation.operation_id.like(item.delivery_id + ":%")
                )
            )
            db.delete(item)
        db.commit()


class ChannelRelayReaper:
    def __init__(self, factory):
        self.factory, self.task = factory, None

    def start(self):
        self.task = asyncio.create_task(self.loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def loop(self):
        while True:
            try:
                await expire_deliveries(self.factory)
            except Exception:
                logger.warning("desktop channel expiry sweep unavailable")
            await asyncio.sleep(15)
