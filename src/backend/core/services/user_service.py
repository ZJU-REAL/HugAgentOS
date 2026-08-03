"""User-related business logic."""

import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Set

from core.auth.account_policy import AccountCapacityExceeded, account_capacity_block_reason
from core.db.models import UserShadow
from core.db.repository import AuditLogRepository, UserRepository
from sqlalchemy.orm import Session


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
        avatar_url: Optional[str] = None,
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
            update_data = {"username": username, "email": email, "last_sync_at": datetime.utcnow()}
            if avatar_url:
                update_data["avatar_url"] = avatar_url
            return self.repo.update(user.user_id, update_data)
        else:
            # Edition policy may cap account creation. Existing users are never
            # blocked by this admission check.
            block_reason = account_capacity_block_reason(self.db)
            if block_reason:
                raise AccountCapacityExceeded(block_reason)

            # Create new user shadow
            user_data = {
                "user_id": f"user_{uuid.uuid4().hex[:16]}",
                "user_center_id": user_center_id,
                "username": username,
                "email": email,
                "avatar_url": avatar_url,
                "last_sync_at": datetime.utcnow(),
            }
            user = self.repo.create(user_data)

            # Audit log
            self.audit_repo.create(
                {
                    "user_id": user.user_id,
                    "action": "user.created",
                    "resource_type": "user",
                    "resource_id": user.user_id,
                    "status": "success",
                }
            )

            return user

    def get_user_settings(self, user_id: str) -> Dict[str, Any]:
        """Read preferences and apply effective memory capability defaults."""
        user = self.repo.get_by_id(user_id)
        if not user:
            return {}
        metadata = dict(user.extra_data) if user.extra_data else {}

        # Imported lazily to keep the services package's compatibility exports
        # from creating an import cycle during application startup.
        from core.services.memory_settings_service import MemorySettingsService

        return MemorySettingsService(self.db).apply_effective_defaults(metadata)

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

    def get_disabled_builtin_subagent_ids(self, user_id: str) -> Set[str]:
        """Return the valid platform sub-agent IDs disabled by this user."""
        user = self.repo.get_by_id(user_id)
        if not user:
            return set()
        metadata = dict(user.extra_data or {})
        raw_ids = metadata.get("disabled_builtin_subagent_ids") or []
        if not isinstance(raw_ids, list):
            return set()

        from core.llm.builtin_subagents import get_builtin_subagent

        return {
            agent_id
            for value in raw_ids
            if isinstance(value, str)
            for agent_id in [value.strip()]
            if agent_id and get_builtin_subagent(agent_id) is not None
        }

    def set_builtin_subagent_enabled(self, user_id: str, agent_id: str, enabled: bool) -> None:
        """Persist one user's platform sub-agent switch; defaults stay enabled."""
        from core.llm.builtin_subagents import get_builtin_subagent

        if get_builtin_subagent(agent_id) is None:
            raise LookupError(f"Builtin sub-agent {agent_id} not found")

        user = self.repo.get_by_id(user_id)
        if not user:
            raise LookupError(f"User {user_id} not found")

        disabled_ids = self.get_disabled_builtin_subagent_ids(user_id)
        if enabled:
            disabled_ids.discard(agent_id)
        else:
            disabled_ids.add(agent_id)

        metadata = dict(user.extra_data or {})
        metadata["disabled_builtin_subagent_ids"] = sorted(disabled_ids)
        self.repo.update(user_id, {"extra_data": metadata})
