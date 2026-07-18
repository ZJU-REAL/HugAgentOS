"""User-related business logic."""

from typing import Optional, Dict, Any
from datetime import datetime
import uuid
from sqlalchemy.orm import Session

from core.db.repository import UserRepository, AuditLogRepository
from core.db.models import UserShadow
from core.licensing import SeatLimitExceeded
from core.licensing.seats import seat_block_reason


class UserService:
    """Service for user-related operations."""

    def __init__(self, db: Session):
        self.db = db
        self.repo = UserRepository(db)
        self.audit_repo = AuditLogRepository(db)

    def get_or_create_user_shadow(
        self,
        user_center_id: str,
        username: str,
        email: Optional[str] = None,
        avatar_url: Optional[str] = None
    ) -> UserShadow:
        """
        Lazy load user shadow from user center.
        Creates shadow if not exists, updates if exists.
        """
        # users_shadow.email has a UNIQUE constraint. In PG multiple NULLs do not
        # conflict, but multiple '' would collide. Add a fallback here to normalize
        # empty/blank strings to None.
        if isinstance(email, str):
            email = email.strip() or None

        user = self.repo.get_by_user_center_id(user_center_id)

        if user:
            # Update existing user
            # Note: avatar_url is only updated when SSO returns a non-empty value——
            # the user may have set their own avatar in SettingsModal, and SSO returns
            # None in most scenarios; we must not overwrite the user's custom avatar with None.
            update_data = {
                "username": username,
                "email": email,
                "last_sync_at": datetime.utcnow()
            }
            if avatar_url:
                update_data["avatar_url"] = avatar_url
            return self.repo.update(user.user_id, update_data)
        else:
            # License seat cap (M4): SSO/external-auth auto account creation is also
            # subject to the seat constraint—— only "new creation" is blocked; existing
            # users' logins are unaffected. The domain exception is rendered as 402 by error_handler.
            block_reason = seat_block_reason(self.db)
            if block_reason:
                raise SeatLimitExceeded(block_reason)

            # Create new user shadow
            user_data = {
                "user_id": f"user_{uuid.uuid4().hex[:16]}",
                "user_center_id": user_center_id,
                "username": username,
                "email": email,
                "avatar_url": avatar_url,
                "last_sync_at": datetime.utcnow()
            }
            user = self.repo.create(user_data)

            # Audit log
            self.audit_repo.create({
                "user_id": user.user_id,
                "action": "user.created",
                "resource_type": "user",
                "resource_id": user.user_id,
                "status": "success"
            })

            return user


    def get_user_settings(self, user_id: str) -> Dict[str, Any]:
        """Read memory/preferences from users_shadow.metadata JSONB."""
        user = self.repo.get_by_id(user_id)
        if not user:
            return {}
        return dict(user.extra_data) if user.extra_data else {}

    def update_user_metadata(self, user_id: str, patch: Dict[str, Any]) -> None:
        """Merge `patch` into users_shadow.metadata JSONB (shallow merge)."""
        user = self.repo.get_by_id(user_id)
        if not user:
            return
        current = dict(user.extra_data) if user.extra_data else {}
        current.update(patch)
        user.extra_data = current
        user.updated_at = datetime.utcnow()
        self.db.commit()
