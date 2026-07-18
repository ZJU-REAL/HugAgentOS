"""Data access — inbound channel bots (channel_connections).

Owner service-account model: a bot is bound to owner_user_id, and all inbound messages
run under the owner's identity.
See internal design docs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import desc
from sqlalchemy.orm import Session

from core.db.models import ChannelConnection


class ChannelConnectionRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, channel_id: str) -> Optional[ChannelConnection]:
        return (
            self.db.query(ChannelConnection)
            .filter(ChannelConnection.channel_id == channel_id)
            .first()
        )

    def get_by_app_id(self, channel_type: str, app_id: str) -> Optional[ChannelConnection]:
        return (
            self.db.query(ChannelConnection)
            .filter(
                ChannelConnection.channel_type == channel_type,
                ChannelConnection.app_id == app_id,
            )
            .first()
        )

    def list_by_owner(
        self,
        owner_user_id: str,
        *,
        agent_id: Optional[str] = None,
        main_only: bool = False,
    ) -> List[ChannelConnection]:
        """List an owner's bots. ``agent_id`` → only those bound to that sub-agent;
        ``main_only`` → only those not bound to any sub-agent (the main agent). Neither
        given → all."""
        q = self.db.query(ChannelConnection).filter(
            ChannelConnection.owner_user_id == owner_user_id
        )
        if agent_id is not None:
            q = q.filter(ChannelConnection.agent_id == agent_id)
        elif main_only:
            q = q.filter(ChannelConnection.agent_id.is_(None))
        return q.order_by(desc(ChannelConnection.created_at)).all()

    def list_active(self) -> List[ChannelConnection]:
        """All enabled and not-disconnected connections — used at startup to build long-lived connections and for scheduling."""
        return (
            self.db.query(ChannelConnection)
            .filter(
                ChannelConnection.enabled.is_(True),
                ChannelConnection.status != "disconnected",
            )
            .all()
        )

    def create(self, data: Dict[str, Any]) -> ChannelConnection:
        item = ChannelConnection(**data)
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        return item

    def update(self, channel_id: str, data: Dict[str, Any]) -> Optional[ChannelConnection]:
        item = self.get_by_id(channel_id)
        if not item:
            return None
        for key, value in data.items():
            setattr(item, key, value)
        self.db.commit()
        self.db.refresh(item)
        return item

    def set_status(
        self, channel_id: str, status: str, *, last_error: Optional[str] = None
    ) -> None:
        item = self.get_by_id(channel_id)
        if not item:
            return
        item.status = status
        if last_error is not None:
            item.last_error = last_error
        if status == "connected":
            item.last_error = None
        self.db.commit()

    def touch_event(self, channel_id: str) -> None:
        item = self.get_by_id(channel_id)
        if not item:
            return
        item.last_event_at = datetime.utcnow()
        self.db.commit()

    def delete(self, channel_id: str) -> bool:
        """Physical delete — bot credentials have no retention value, so delete outright after disconnect (not a soft delete, releases the token lock)."""
        item = self.get_by_id(channel_id)
        if not item:
            return False
        self.db.delete(item)
        self.db.commit()
        return True
