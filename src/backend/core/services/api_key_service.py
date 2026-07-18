"""Per-user API-Key business logic.

API-Keys are used to call the agent over HTTP as the user (equivalent to a logged-in session, inheriting all the user's capabilities).
Security model:
  - The plaintext looks like ``sk-jx-<random>``, returned only once at creation; the DB stores only the SHA256 hash + prefix.
  - Calls carry ``Authorization: Bearer sk-jx-...``; the auth layer calls ``resolve_api_key``
    to look up by hash and verify enabled status / expiry / the user capability bit ``can_use_api_key``.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from core.db.models import UserApiKey, UserShadow

API_KEY_PREFIX = "sk-jx-"

# last_used_at write throttle: consecutive calls of the same key within this window are not persisted repeatedly, avoiding a write on every chat request.
_LAST_USED_THROTTLE_S = 60.0


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ApiKeyService:
    """Service for personal API-Key CRUD + verification."""

    def __init__(self, db: Session):
        self.db = db

    # ── CRUD ────────────────────────────────────────────────────────────────

    def list_keys(self, user_id: str) -> List[UserApiKey]:
        """Return all of a user's non-revoked keys (newest first by creation time)."""
        return (
            self.db.query(UserApiKey)
            .filter(UserApiKey.user_id == user_id, UserApiKey.revoked_at.is_(None))
            .order_by(UserApiKey.created_at.desc())
            .all()
        )

    def create_key(
        self,
        user_id: str,
        name: str,
        expires_in_days: Optional[int] = None,
    ) -> Tuple[UserApiKey, str]:
        """Generate a new key. Returns (ORM row, plaintext); the plaintext is visible only this once.

        ``expires_in_days=None`` means never expires; otherwise it expires N days from the current time.
        """
        raw = API_KEY_PREFIX + secrets.token_urlsafe(32)
        prefix = raw[:14]  # sk-jx- + first 8 random chars, for listing display
        expires_at: Optional[datetime] = None
        if expires_in_days is not None and expires_in_days > 0:
            expires_at = _now() + timedelta(days=expires_in_days)

        from core.infra.crypto import encrypt_secret

        row = UserApiKey(
            id=f"ak_{uuid.uuid4().hex[:20]}",
            user_id=user_id,
            name=(name or "API Key").strip()[:128],
            key_prefix=prefix,
            key_hash=_hash_key(raw),
            key_enc=encrypt_secret(raw),  # reversible ciphertext, decrypted on demand for "copy again"
            enabled=True,
            expires_at=expires_at,
            created_at=_now(),
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row, raw

    def get_key(self, user_id: str, key_id: str) -> Optional[UserApiKey]:
        """Get a single non-revoked key of a user (nonexistent / not theirs / already revoked → None)."""
        return (
            self.db.query(UserApiKey)
            .filter(
                UserApiKey.id == key_id,
                UserApiKey.user_id == user_id,
                UserApiKey.revoked_at.is_(None),
            )
            .first()
        )

    def set_enabled(self, user_id: str, key_id: str, enabled: bool) -> Optional[UserApiKey]:
        row = (
            self.db.query(UserApiKey)
            .filter(
                UserApiKey.id == key_id,
                UserApiKey.user_id == user_id,
                UserApiKey.revoked_at.is_(None),
            )
            .first()
        )
        if not row:
            return None
        row.enabled = enabled
        self.db.commit()
        self.db.refresh(row)
        return row

    def revoke_key(self, user_id: str, key_id: str) -> bool:
        """Soft delete: mark revoked_at. Once revoked, the key is immediately invalid and no longer appears in listings."""
        row = (
            self.db.query(UserApiKey)
            .filter(
                UserApiKey.id == key_id,
                UserApiKey.user_id == user_id,
                UserApiKey.revoked_at.is_(None),
            )
            .first()
        )
        if not row:
            return False
        row.revoked_at = _now()
        row.enabled = False
        self.db.commit()
        return True


def is_api_key_token(token: Optional[str]) -> bool:
    """Whether the token is in API-Key form (for fast routing at the auth layer)."""
    return bool(token) and token.startswith(API_KEY_PREFIX)


def resolve_api_key(db: Session, raw: str) -> Optional[UserShadow]:
    """Look up the user by plaintext key.

    Returns UserShadow if it passes validation (enabled / not revoked / not expired / user capability bit can_use_api_key),
    otherwise None. On a hit, throttle-updates last_used_at along the way.
    """
    if not is_api_key_token(raw):
        return None

    row = (
        db.query(UserApiKey)
        .filter(
            UserApiKey.key_hash == _hash_key(raw),
            UserApiKey.revoked_at.is_(None),
        )
        .first()
    )
    if not row or not row.enabled:
        return None

    # Expiry check (expires_at None means never expires)
    if row.expires_at is not None:
        exp = row.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < _now():
            return None

    user = db.query(UserShadow).filter(UserShadow.user_id == row.user_id).first()
    if not user:
        return None

    # Admins can invalidate all of a user's keys instantly at any time by turning off can_use_api_key (personal explicit → team default → off by default)
    from core.auth.capabilities import resolve_user_capabilities

    if not resolve_user_capabilities(db, str(row.user_id))["can_use_api_key"]:
        return None

    # Throttled write of last_used_at
    last = row.last_used_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if last is None or (_now() - last).total_seconds() > _LAST_USED_THROTTLE_S:
        try:
            row.last_used_at = _now()
            db.commit()
        except Exception:
            db.rollback()

    return user
