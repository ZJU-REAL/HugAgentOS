"""One-time ticket store for OA redirect login.

Used by the "backend exchanges ticket + browser redirect" flow: the OA backend
server calls ``/v1/auth/oa/login`` to obtain a ticket, then redirects the user's
browser to ``/v1/auth/oa/callback?ticket=...`` to exchange it for a session cookie.

Ticket properties (security-critical):
  - **Irreversible**: random token, carries no plaintext user information;
  - **Single use**: atomic GET+DEL on consume, discarded once used, prevents replay;
  - **Second-level expiry**: TTL controlled by ``OA_SSO_TICKET_TTL_SECONDS`` (default 60s).

Key format: ``jx:oa_ticket:{sha256(token)}`` — consistent with session, stores the
hash in the store rather than the raw value.
Degrades to in-process storage when the backend has no Redis (same approach as
session.py, dev/single-instance only).
"""

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from core.config.settings import settings
from core.infra.logging import get_logger

logger = get_logger(__name__)

TICKET_KEY_PREFIX = "jx:oa_ticket:"
_MEMORY_TICKETS: dict[str, dict[str, Any]] = {}


def _ttl_seconds() -> int:
    return int(settings.oa_sso.ticket_ttl_seconds)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _use_memory_store() -> bool:
    return settings.session.store_type == "memory"


def _prune_expired_memory() -> None:
    now = datetime.now(timezone.utc)
    for key in [k for k, v in _MEMORY_TICKETS.items() if v.get("expires_at") and v["expires_at"] <= now]:
        _MEMORY_TICKETS.pop(key, None)


async def issue_ticket(payload: Dict[str, Any]) -> str:
    """Issue a one-time ticket, return the raw token (placed in the redirect URL's ?ticket=).

    ``payload`` only needs the minimal information required to exchange for a session
    (e.g. internal user_id, dept_id).
    """
    token = secrets.token_urlsafe(32)
    key = _hash(token)
    ttl = _ttl_seconds()
    body = dict(payload)
    body["issued_at"] = datetime.now(timezone.utc).isoformat()

    if _use_memory_store():
        _prune_expired_memory()
        _MEMORY_TICKETS[key] = {
            "payload": body,
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=ttl),
        }
        logger.info("oa_ticket_issued", user_id=body.get("user_id"), ttl=ttl, store="memory")
        return token

    from core.infra.redis import get_redis
    r = get_redis()
    await r.set(f"{TICKET_KEY_PREFIX}{key}", json.dumps(body, ensure_ascii=False), ex=ttl)
    logger.info("oa_ticket_issued", user_id=body.get("user_id"), ttl=ttl)
    return token


async def consume_ticket(token: str) -> Optional[Dict[str, Any]]:
    """Atomically consume a ticket: delete on hit (single use). Returns payload or None (invalid/expired/already used)."""
    if not token:
        return None
    key = _hash(token)

    if _use_memory_store():
        _prune_expired_memory()
        entry = _MEMORY_TICKETS.pop(key, None)
        if entry is None:
            return None
        return dict(entry["payload"])

    from core.infra.redis import get_redis
    r = get_redis()
    full = f"{TICKET_KEY_PREFIX}{key}"
    # GETDEL atomically fetches and deletes (redis ≥ 6.2); prevents concurrent replay
    try:
        raw = await r.getdel(full)
    except AttributeError:  # old client lacks getdel: degrade to get + delete
        raw = await r.get(full)
        if raw is not None:
            await r.delete(full)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("oa_ticket_corrupt", token_hash=key[:8])
        return None
