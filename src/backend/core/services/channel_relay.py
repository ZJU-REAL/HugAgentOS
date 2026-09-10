"""Cloud queue and authorization for desktop-owned channel execution."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict

from core.db.models import ChannelConnection
from core.db.models.channel_relay import ChannelRelayDelivery, DesktopChannelBinding
from core.infra.exceptions import AccessDeniedError, BadRequestError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

ONLINE_SECONDS = 35
LEASE_SECONDS = 45
MAX_PENDING = 64


def execution(conn):
    return (conn.config or {}).get("desktop_execution") or {}


def public_execution(conn):
    binding = execution(conn)
    return {
        "execution_location": "local" if binding else "cloud",
        "device_name": binding.get("device_name"),
        "device_id": binding.get("device_id"),
    }


class ChannelRelayService:
    def __init__(self, db):
        self.db = db

    def register(self, owner, device, name):
        if not device or len(device) > 128:
            raise BadRequestError("设备身份无效")
        row = DesktopChannelBinding(
            binding_id=uuid.uuid4().hex,
            owner_id=owner,
            device_id=device,
            device_name=(name or "本机")[:100],
            consumed=False,
            created_at=time.time(),
            last_seen=time.time(),
        )
        self.db.add(row)
        self.db.commit()
        return {"binding_id": row.binding_id, "device_id": device, "device_name": row.device_name}

    def bind(self, owner, binding_id):
        row = self.db.get(DesktopChannelBinding, binding_id)
        if not row or row.owner_id != owner or row.created_at < time.time() - 600:
            raise BadRequestError("本机授权已失效，请在桌面端重新选择本机")
        changed = self.db.execute(
            update(DesktopChannelBinding)
            .where(
                DesktopChannelBinding.binding_id == binding_id,
                DesktopChannelBinding.consumed.is_(False),
            )
            .values(consumed=True)
        )
        if changed.rowcount != 1:
            raise BadRequestError("本机授权已使用，请重新选择本机")
        # The caller commits with the channel row, including QR confirmation.
        return {
            "binding_id": binding_id,
            "device_id": row.device_id,
            "device_name": row.device_name,
        }

    def online(self, binding_id):
        row = self.db.get(DesktopChannelBinding, binding_id)
        return bool(row and row.last_seen >= time.time() - ONLINE_SECONDS)

    def enqueue(self, conn, msg):
        binding = execution(conn)
        raw = asdict(msg)
        data = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        if len(data.encode()) > 1024 * 1024:
            raise BadRequestError("渠道消息过大")
        key = hashlib.sha256(
            (conn.channel_id + ":" + (msg.message_id or data)).encode()
        ).hexdigest()
        if self.db.get(ChannelRelayDelivery, key):
            return key
        if not self.online(binding["binding_id"]):
            raise BadRequestError("本机设备离线，请打开桌面端并登录后重发消息")
        pending = self.db.scalar(
            select(func.count())
            .select_from(ChannelRelayDelivery)
            .where(
                ChannelRelayDelivery.binding_id == binding["binding_id"],
                ChannelRelayDelivery.status.in_(["pending", "running"]),
            )
        )
        if pending >= MAX_PENDING:
            raise BadRequestError("本机机器人繁忙，请稍后重试")
        self.db.add(
            ChannelRelayDelivery(
                delivery_id=key,
                channel_id=conn.channel_id,
                binding_id=binding["binding_id"],
                owner_id=conn.owner_user_id,
                device_id=binding["device_id"],
                status="pending",
                payload={
                    "message": raw,
                    "bot": {
                        "channel_type": conn.channel_type,
                        "display_name": conn.display_name,
                        "agent_id": conn.agent_id,
                        "resource_scope": conn.resource_scope,
                        "group_listen_mode": conn.group_listen_mode,
                    },
                },
                created_at=time.time(),
                deadline=time.time() + 300,
            )
        )
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            if not self.db.get(ChannelRelayDelivery, key):
                raise
        return key

    def retained_bindings(self, owner, device, bindings):
        attached = {
            execution(conn).get("binding_id")
            for conn in self.db.scalars(
                select(ChannelConnection).where(ChannelConnection.owner_user_id == owner)
            ).all()
        }
        return [
            row.binding_id
            for row in self.db.scalars(
                select(DesktopChannelBinding).where(
                    DesktopChannelBinding.owner_id == owner,
                    DesktopChannelBinding.device_id == device,
                    DesktopChannelBinding.binding_id.in_(bindings),
                )
            ).all()
            if row.binding_id in attached
            or (not row.consumed and row.created_at > time.time() - 600)
        ]

    def heartbeat(self, owner, device, bindings):
        self.db.execute(
            update(DesktopChannelBinding)
            .where(
                DesktopChannelBinding.owner_id == owner,
                DesktopChannelBinding.device_id == device,
                DesktopChannelBinding.binding_id.in_(bindings),
            )
            .values(last_seen=time.time())
        )
        self.db.commit()

    def claim(self, owner, device, bindings):
        self.heartbeat(owner, device, bindings)
        running = self.db.scalar(
            select(ChannelRelayDelivery.delivery_id)
            .where(
                ChannelRelayDelivery.owner_id == owner,
                ChannelRelayDelivery.device_id == device,
                ChannelRelayDelivery.status == "running",
                ChannelRelayDelivery.deadline > time.time(),
            )
            .limit(1)
        )
        if running:
            return None
        query = (
            select(ChannelRelayDelivery)
            .where(
                ChannelRelayDelivery.owner_id == owner,
                ChannelRelayDelivery.device_id == device,
                ChannelRelayDelivery.binding_id.in_(bindings),
                ChannelRelayDelivery.status == "pending",
                ChannelRelayDelivery.deadline > time.time(),
            )
            .order_by(ChannelRelayDelivery.created_at)
            .limit(1)
        )
        row = self.db.scalar(query)
        if row is None:
            return None
        conn = self.db.get(ChannelConnection, row.channel_id)
        if not conn or not conn.enabled or execution(conn).get("binding_id") != row.binding_id:
            row.status = "cancelled"
            self.db.commit()
            return None
        lease = uuid.uuid4().hex
        changed = self.db.execute(
            update(ChannelRelayDelivery)
            .where(
                ChannelRelayDelivery.delivery_id == row.delivery_id,
                ChannelRelayDelivery.status == "pending",
            )
            .values(status="running", lease=lease, deadline=time.time() + LEASE_SECONDS)
        )
        self.db.commit()
        if changed.rowcount != 1:
            return None
        return {
            "delivery_id": row.delivery_id,
            "binding_id": row.binding_id,
            "lease": lease,
            **row.payload,
        }

    def authorized(self, owner, device, delivery_id, lease):
        row = self.db.get(ChannelRelayDelivery, delivery_id)
        conn = self.db.get(ChannelConnection, row.channel_id) if row else None
        if (
            not row
            or row.owner_id != owner
            or row.device_id != device
            or row.lease != lease
            or row.status != "running"
            or row.deadline < time.time()
            or not conn
            or not conn.enabled
            or execution(conn).get("binding_id") != row.binding_id
        ):
            raise AccessDeniedError("本机机器人任务已失效")
        return row, conn

    def renew(self, owner, device, delivery_id, lease):
        row, _ = self.authorized(owner, device, delivery_id, lease)
        if row.created_at < time.time() - 3600:
            raise AccessDeniedError("本机机器人任务超时")
        row.deadline = time.time() + LEASE_SECONDS
        self.db.execute(
            update(DesktopChannelBinding)
            .where(
                DesktopChannelBinding.owner_id == owner, DesktopChannelBinding.device_id == device
            )
            .values(last_seen=time.time())
        )
        self.db.commit()

    def complete(self, owner, device, delivery_id, lease, error=None):
        row, _ = self.authorized(owner, device, delivery_id, lease)
        row.status = "failed" if error else "done"
        row.error = error[:300] if error else None
        self.db.commit()
