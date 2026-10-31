"""Session management backed by Redis.

Key format:  jx:session:{sha256(token)}
Value:       JSON-encoded user data
TTL:         SESSION_TTL_HOURS (default 8h)

``SESSION_STORE=memory`` keeps the table in process instead. With
``SESSION_PERSIST_PATH`` set, that table is mirrored to a file so the desktop
local server, which restarts with the desktop app, keeps its logins.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from core.config.settings import settings
from core.infra.logging import get_logger
from core.infra.redis import get_redis

logger = get_logger(__name__)

SESSION_KEY_PREFIX = "jx:session:"

# Sliding renewal touches every request; the file only needs to follow it often
# enough that a restart does not cost a session its last few minutes of life.
_PERSIST_RENEWAL_INTERVAL_SECONDS = 300.0
_PERSIST_WRITE_LOCK = threading.Lock()


def _ttl_seconds() -> int:
    return int(settings.session.ttl_hours * 3600)


def remember_ttl_seconds() -> int:
    """Session "remember me" duration (seconds).

    The number of days is adjusted by the admin under "System Config → Login & Sessions"
    (``auth.remember_me_days``), defaulting to 30 days. On read failure or invalid config, falls back to 30 days.
    """
    days = 30.0
    try:
        from core.config.store.system_config import SystemConfigService

        raw = SystemConfigService.get_instance().get("auth.remember_me_days", "30")
        if raw:
            days = float(raw)
    except (
        Exception
    ):  # noqa: BLE001 — on config unavailable, silently fall back to the default value, don't block login
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


def _persist_path() -> Optional[Path]:
    raw = settings.session.persist_path
    return Path(raw) if raw else None


def _read_persisted(path: Path) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("session_persist_unreadable", path=str(path), error=str(exc))
        return {}
    now = datetime.now(timezone.utc)
    sessions: dict[str, dict[str, Any]] = {}
    for token_hash, entry in (raw.items() if isinstance(raw, dict) else ()):
        try:
            payload = entry["payload"]
            expires_at = datetime.fromisoformat(entry["expires_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(payload, dict) and expires_at > now:
            sessions[token_hash] = {"payload": payload, "expires_at": expires_at}
    return sessions


def _memory_sessions() -> dict[str, dict[str, Any]]:
    """The in-process session table, loaded from the persist file on first use."""
    state = process_state()
    path = _persist_path()
    if path is not None and not state.persist_loaded:
        state.persist_loaded = True
        state.MEMORY_SESSIONS.update(_read_persisted(path))
    return state.MEMORY_SESSIONS


def _write_persisted(path: Path, snapshot: dict[str, Any], generation: int) -> None:
    with _PERSIST_WRITE_LOCK:
        state = process_state()
        # Snapshots are taken in order on the event loop but written from worker
        # threads; never let an older one overwrite a newer one.
        if generation <= state.persist_written_generation:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        state.persist_written_generation = generation


async def _persist_memory_sessions() -> None:
    path = _persist_path()
    if path is None:
        return
    state = process_state()
    state.persist_generation += 1
    state.persisted_at = time.monotonic()
    snapshot = {
        token_hash: {"payload": entry["payload"], "expires_at": entry["expires_at"].isoformat()}
        for token_hash, entry in _memory_sessions().items()
    }
    try:
        await asyncio.to_thread(_write_persisted, path, snapshot, state.persist_generation)
    except OSError as exc:
        # The session itself is valid in memory; only surviving a restart is lost.
        logger.warning("session_persist_failed", path=str(path), error=str(exc))


def _prune_expired_memory_sessions() -> None:
    now = datetime.now(timezone.utc)
    sessions = _memory_sessions()
    expired = [
        key
        for key, value in sessions.items()
        if value.get("expires_at") and value["expires_at"] <= now
    ]
    for key in expired:
        sessions.pop(key, None)


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
        _memory_sessions()[token_hash] = {
            "payload": payload,
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=ttl),
        }
        await _persist_memory_sessions()
        logger.info("session_created", user_id=payload["user_id"], ttl=ttl, store="memory")
        return token

    r = get_redis()
    await r.set(
        f"{SESSION_KEY_PREFIX}{token_hash}", json.dumps(payload, ensure_ascii=False), ex=ttl
    )

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
        entry = _memory_sessions().get(token_hash)
        if entry is None:
            return None
        # Sliding renewal uses the session's own TTL (a long "remember me" session is not truncated to the default duration)
        renew_ttl = _resolve_ttl(entry["payload"].get("ttl_seconds"))
        entry["expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=renew_ttl)
        elapsed = time.monotonic() - process_state().persisted_at
        if elapsed >= _PERSIST_RENEWAL_INTERVAL_SECONDS:
            await _persist_memory_sessions()
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


async def find_session_by_hash(token_hash: str) -> Optional[Dict[str, Any]]:
    """Read an existing session for a derived token without renewing its lifetime.

    The digest is an identifier, never accepted as a login cookie. Capability
    requests must not keep the shell's login session alive on their own.
    """
    if len(token_hash) != 64 or any(c not in "0123456789abcdef" for c in token_hash):
        return None
    if _use_memory_store():
        _prune_expired_memory_sessions()
        entry = _memory_sessions().get(token_hash)
        return dict(entry["payload"]) if entry else None
    raw = await get_redis().get(f"{SESSION_KEY_PREFIX}{token_hash}")
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


async def revoke_session(token: str) -> bool:
    """Revoke (delete) a session.

    Returns:
        True if a session was actually deleted.
    """
    token_hash = _hash_token(token)
    data = await find_session_by_hash(token_hash)
    if _use_memory_store():
        deleted = _memory_sessions().pop(token_hash, None) is not None
        if deleted:
            await _persist_memory_sessions()
    else:
        deleted = bool(await get_redis().delete(f"{SESSION_KEY_PREFIX}{token_hash}"))
    if deleted:
        logger.info("session_revoked", token_hash=token_hash[:8])
        if data and data.get("user_id"):
            from core.harness.registry.resource_owners import resource_owner
            try:
                await resource_owner().revoke_session(data["user_id"], token_hash)
            except Exception as exc:
                # The credential is already revoked. Preserve successful logout;
                # expired workers stay denied while the resource janitor retries.
                logger.warning(
                    "workbench_logout_cleanup_pending",
                    user_id=data["user_id"],
                    error_type=type(exc).__name__,
                )
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


@dataclass
class SessionState:
    MEMORY_SESSIONS: dict[str, dict[str, Any]] = field(default_factory=lambda: {})
    persist_loaded: bool = False
    persisted_at: float = 0.0
    persist_generation: int = 0
    persist_written_generation: int = 0


_STATE = SessionState()


def process_state() -> SessionState:
    return _STATE
