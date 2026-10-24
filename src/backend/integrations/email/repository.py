"""Email connection persistence metadata."""
from typing import Any, Dict, Optional
from sqlalchemy.orm import Session
from core.infra.time import utc_now
from integrations.email.models import EmailConnection

class EmailConnectionRepository:
    """Per-user email connection (email_connections) CRUD. 1:1 with users_shadow, no soft delete.
    Structurally identical to [[LarkConnectionRepository]]."""

    def __init__(self, db: Session):
        self.db = db

    def get(self, user_id: str) -> Optional[EmailConnection]:
        return self.db.query(EmailConnection).filter(EmailConnection.user_id == user_id).first()

    def ensure(self, user_id: str) -> EmailConnection:
        """Idempotent: return existing record, otherwise create a disconnected record."""
        record = self.get(user_id)
        if record:
            return record
        record = EmailConnection(user_id=user_id, status="disconnected")
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def update(self, user_id: str, data: Dict[str, Any]) -> Optional[EmailConnection]:
        record = self.get(user_id)
        if not record:
            return None
        for k, v in data.items():
            setattr(record, k, v)
        record.updated_at = utc_now()
        self.db.commit()
        self.db.refresh(record)
        return record
