"""Data access layer — user repositories.

Split out of the former monolithic ``core/db/repository.py``. The package
``__init__`` re-exports every repository class, so ``from core.db.repository
import XxxRepository`` keeps working unchanged.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from core.db.models import (
    DingTalkConnection,
    EmailConnection,
    LarkConnection,
    LocalUser,
    UserShadow,
)
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.orm import Session


class UserRepository:
    """Repository for user operations."""

    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, user_id: str) -> Optional[UserShadow]:
        """Get user by ID."""
        return self.db.query(UserShadow).filter(UserShadow.user_id == user_id).first()

    def get_by_user_center_id(self, user_center_id: str) -> Optional[UserShadow]:
        """Get user by user center ID."""
        return self.db.query(UserShadow).filter(UserShadow.user_center_id == user_center_id).first()

    def create(self, user_data: Dict[str, Any]) -> UserShadow:
        """Create a new user shadow."""
        user = UserShadow(**user_data)
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user

    def update(self, user_id: str, update_data: Dict[str, Any]) -> Optional[UserShadow]:
        """Update user information."""
        user = self.get_by_id(user_id)
        if not user:
            return None

        for key, value in update_data.items():
            setattr(user, key, value)

        user.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(user)
        return user


class LocalUserRepository:
    """Local account Repository."""

    def __init__(self, db: Session):
        self.db = db

    def get(self, user_id: str) -> Optional[LocalUser]:
        return self.db.query(LocalUser).filter(LocalUser.user_id == user_id).first()

    def get_by_username(self, username: str) -> Optional[tuple[LocalUser, UserShadow]]:
        """Join-query LocalUser + UserShadow by username."""
        row = (
            self.db.query(LocalUser, UserShadow)
            .join(UserShadow, LocalUser.user_id == UserShadow.user_id)
            .filter(UserShadow.username == username)
            .first()
        )
        # Return a real tuple (SQLAlchemy 2.0's Row is not a tuple subclass, so this makes the
        # `-> tuple[...]` annotation honest). The whole class of "annotation type vs runtime object"
        # mismatches is fixed at the root in docker/cython_build.py (compiled image sets global
        # annotation_typing=False); converting again here is defensive — plaintext CPython also gets
        # a real tuple, and even if that compile flag stops working someday, it won't reintroduce
        # "Expected tuple, got Row" (which would swallow login into "wrong username or password").
        return tuple(row) if row is not None else None

    def get_by_email(self, email: str) -> Optional[tuple[LocalUser, UserShadow]]:
        """Join-query LocalUser + UserShadow by email."""
        if not email:
            return None
        row = (
            self.db.query(LocalUser, UserShadow)
            .join(UserShadow, LocalUser.user_id == UserShadow.user_id)
            .filter(UserShadow.email == email)
            .first()
        )
        # Same as get_by_username: return a real tuple (defensive layer, see reasoning above).
        return tuple(row) if row is not None else None

    def create(self, data: Dict[str, Any]) -> LocalUser:
        record = LocalUser(**data)
        self.db.add(record)
        self.db.flush()  # caller commits
        return record

    def update(self, user_id: str, data: Dict[str, Any]) -> Optional[LocalUser]:
        record = self.get(user_id)
        if not record:
            return None
        for k, v in data.items():
            setattr(record, k, v)
        record.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(record)
        return record

    def set_status(self, user_id: str, status: str) -> bool:
        record = self.get(user_id)
        if not record:
            return False
        record.status = status
        record.updated_at = datetime.utcnow()
        self.db.commit()
        return True


class DingTalkConnectionRepository:
    """Per-user DingTalk connection (dingtalk_connections) CRUD. 1:1 with users_shadow, no soft delete."""

    def __init__(self, db: Session):
        self.db = db

    def get(self, user_id: str) -> Optional[DingTalkConnection]:
        return (
            self.db.query(DingTalkConnection).filter(DingTalkConnection.user_id == user_id).first()
        )

    def ensure(self, user_id: str) -> DingTalkConnection:
        """Idempotent: return existing record, otherwise create a disconnected record."""
        record = self.get(user_id)
        if record:
            return record
        record = DingTalkConnection(user_id=user_id, status="disconnected")
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def update(self, user_id: str, data: Dict[str, Any]) -> Optional[DingTalkConnection]:
        record = self.get(user_id)
        if not record:
            return None
        for k, v in data.items():
            setattr(record, k, v)
        record.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(record)
        return record


class LarkConnectionRepository:
    """Per-user Lark connection (lark_connections) CRUD. 1:1 with users_shadow, no soft delete.
    Structurally identical to [[DingTalkConnectionRepository]]."""

    def __init__(self, db: Session):
        self.db = db

    def get(self, user_id: str) -> Optional[LarkConnection]:
        return self.db.query(LarkConnection).filter(LarkConnection.user_id == user_id).first()

    def ensure(self, user_id: str) -> LarkConnection:
        """Idempotent: return existing record, otherwise create a disconnected record."""
        record = self.get(user_id)
        if record:
            return record
        record = LarkConnection(user_id=user_id, status="disconnected")
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def update(self, user_id: str, data: Dict[str, Any]) -> Optional[LarkConnection]:
        record = self.get(user_id)
        if not record:
            return None
        for k, v in data.items():
            setattr(record, k, v)
        record.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(record)
        return record


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
        record.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(record)
        return record
