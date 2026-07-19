"""Session management backed by Redis.

Key format:  jx:session:{sha256(token)}
Value:       JSON-encoded user data
TTL:         SESSION_TTL_HOURS (default 8h)
"""

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from core.config.settings import settings
from core.infra.logging import get_logger
from core.infra.redis import get_redis

logger = get_logger(__name__)

SESSION_KEY_PREFIX = "jx:session:"
_MEMORY_SESSIONS: dict[str, dict[str, Any]] = {}


def _ttl_seconds() -> int:
    return int(settings.session.ttl_hours * 3600)


def remember_ttl_seconds() -> int:
    """Session "remember me" duration (seconds).

    The number of days is adjusted by the admin under "System Config → Login & Sessions"
    (``auth.remember_me_days``), defaulting to 30 days. On read failure or invalid config, falls back to 30 days.
    """
    days = 30.0
    try:
        from core.services.system_config import SystemConfigService

        raw = SystemConfigService.get_instance().get("auth.remember_me_days", "30")
        if raw:
            days = float(raw)
    except Exception:  # noqa: BLE001 — on config unavailable, silently fall back to the default value, don't block login
        days = 30.0
    if days <= 0:
        days = 30.0
    return int(days * 86400)


def _resolve_ttl(ttl_seconds: Optional[int]) -> int:
    """Explicit TTL takes priority (>0), otherwise use the default session duration."""
    if ttl_seconds and ttl_seconds > 0:
        return int(ttl_seconds)
    return _ttl_seconds()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _use_memory_store() -> bool:
    return settings.session.store_type == "memory"


def _prune_expired_memory_sessions() -> None:
    now = datetime.now(timezone.utc)
    expired = [
        key for key, value in _MEMORY_SESSIONS.items()
        if value.get("expires_at") and value["expires_at"] <= now
    ]
    for key in expired:
        _MEMORY_SESSIONS.pop(key, None)


def generate_token() -> str:
    """Generate a cryptographically random session token."""
    return secrets.token_urlsafe(32)


async def create_session(
    user_data: Dict[str, Any],
    ttl_seconds: Optional[int] = None,
) -> str:
    """Create a new session in Redis.

    Args:
        user_data: dict with user_id, user_center_id, username, email, avatar_url
        ttl_seconds: explicit session lifetime (seconds). If omitted, the default session
            duration (``SESSION_TTL_HOURS``) is used; when "remember me" is checked, pass
            :func:`remember_ttl_seconds`. This value is written into the session payload so
            that sliding renewal (:func:`validate_session`) extends life by the same window,
            avoiding a long session being truncated to the default duration on its first refresh.

    Returns:
        The raw session token (to be placed in the Cookie).
    """
    token = generate_token()
    token_hash = _hash_token(token)
    ttl = _resolve_ttl(ttl_seconds)

    payload = {
        "user_id": user_data["user_id"],
        "user_center_id": user_data.get("user_center_id", ""),
        "username": user_data.get("username", ""),
        "email": user_data.get("email"),
        "avatar_url": user_data.get("avatar_url"),
        "sso_token": user_data.get("sso_token"),
        "ttl_seconds": ttl,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    if _use_memory_store():
        _prune_expired_memory_sessions()
        _MEMORY_SESSIONS[token_hash] = {
            "payload": payload,
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=ttl),
        }
        logger.info("session_created", user_id=payload["user_id"], ttl=ttl, store="memory")
        return token

    r = get_redis()
    await r.set(f"{SESSION_KEY_PREFIX}{token_hash}", json.dumps(payload, ensure_ascii=False), ex=ttl)

    logger.info("session_created", user_id=payload["user_id"], ttl=ttl)
    return token


async def validate_session(token: str) -> Optional[Dict[str, Any]]:
    """Validate a session token and return user data if valid.

    Also performs sliding-window TTL renewal.

    Returns:
        User data dict or None if session is invalid/expired.
    """
    token_hash = _hash_token(token)
    key = f"{SESSION_KEY_PREFIX}{token_hash}"

    if _use_memory_store():
        _prune_expired_memory_sessions()
        entry = _MEMORY_SESSIONS.get(token_hash)
        if entry is None:
            return None
        # Sliding renewal uses the session's own TTL (a long "remember me" session is not truncated to the default duration)
        renew_ttl = _resolve_ttl(entry["payload"].get("ttl_seconds"))
        entry["expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=renew_ttl)
        return dict(entry["payload"])

    r = get_redis()
    raw = await r.get(key)
    if raw is None:
        return None

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("session_data_corrupt", token_hash=token_hash[:8])
        await r.delete(key)
        return None

    # Sliding renewal: reset TTL on every successful validation, honouring the
    # session's own window (e.g. 30d for "remember me") so a long session is not
    # silently shrunk to the default TTL on the first refresh.
    await r.expire(key, _resolve_ttl(data.get("ttl_seconds")))
    return data


async def revoke_session(token: str) -> bool:
    """Revoke (delete) a session.

    Returns:
        True if a session was actually deleted.
    """
    token_hash = _hash_token(token)
    if _use_memory_store():
        return _MEMORY_SESSIONS.pop(token_hash, None) is not None

    r = get_redis()
    deleted = await r.delete(f"{SESSION_KEY_PREFIX}{token_hash}")
    if deleted:
        logger.info("session_revoked", token_hash=token_hash[:8])
    return bool(deleted)


def session_cookie_params(ttl_seconds: Optional[int] = None) -> Dict[str, Any]:
    """Return the kwargs for ``response.set_cookie()``.

    ``ttl_seconds`` must match what was used when creating the session, so the cookie ``max_age`` aligns with the Redis TTL.
    """
    ttl = _resolve_ttl(ttl_seconds)

    return {
        "key": settings.session.cookie_name,
        "max_age": ttl,
        "path": "/",
        "secure": settings.session.cookie_secure,
        "httponly": settings.session.cookie_httponly,
        "samesite": settings.session.cookie_samesite,
        "domain": settings.session.cookie_domain,
    }


def expires_at_iso(ttl_seconds: Optional[int] = None) -> str:
    """Calculate and return the expiration time as an ISO string."""
    ttl = _resolve_ttl(ttl_seconds)
    return (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
