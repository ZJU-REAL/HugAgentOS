"""One-time handoff ticket store for the desktop client's "redirect login".

Used for the secure ticket exchange of desktop plan B (system-browser login +
deep-link waking the app):

  Browser-side login succeeds (session cookie already present)
    → POST /v1/auth/desktop/handoff  issues a one-time handoff ticket
    → browser 302 to  hugagent://auth/callback?ticket=<handoff>
    → OS wakes the desktop app
    → app calls HTTPS directly  POST /v1/auth/desktop/redeem {ticket}  to exchange for the real session token

The deep-link URL carries only this **single-use, seconds-lived** handoff
ticket; the long-lived session token never appears in a URL (avoiding capture
by other local programs registered on the same protocol). This is the same
security posture as OA redirect login (:mod:`core.auth.oa_ticket_store`), but
semantically independent and non-interfering: this module uses its own key
prefix and its own TTL, and does not depend on whether OA SSO is enabled.

Ticket properties (security-critical):
  - **Irreversible**: random token, carries no plaintext user info; the store
    keeps only sha256(token);
  - **Single use**: atomic GETDEL on consume — once used, it's dead;
    anti-replay;
  - **Seconds-level expiry**: TTL controlled by
    ``DESKTOP_HANDOFF_TICKET_TTL_SECONDS`` (default 120s).

Without Redis the backend degrades to in-process storage (same approach as
session.py / oa_ticket_store.py; dev / single instance only).
"""

import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from core.config.settings import settings
from core.infra.logging import get_logger

logger = get_logger(__name__)

TICKET_KEY_PREFIX = "jx:desktop_ticket:"
_MEMORY_TICKETS: dict[str, dict[str, Any]] = {}


def ttl_seconds() -> int:
    """Handoff ticket lifetime in seconds. Default 120s, adjustable via env var; invalid values fall back to the default."""
    try:
        val = int(os.getenv("DESKTOP_HANDOFF_TICKET_TTL_SECONDS", "120"))
    except (TypeError, ValueError):
        return 120
    return val if val > 0 else 120


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _use_memory_store() -> bool:
    return settings.session.store_type == "memory"


def _prune_expired_memory() -> None:
    now = datetime.now(timezone.utc)
    for key in [k for k, v in _MEMORY_TICKETS.items() if v.get("expires_at") and v["expires_at"] <= now]:
        _MEMORY_TICKETS.pop(key, None)


async def issue_ticket(payload: Dict[str, Any]) -> str:
    """Issue a one-time handoff ticket and return the raw token (goes into the deep-link's ?ticket=).

    ``payload`` carries only the minimum needed for the token exchange
    (here, ``session_token``).
    """
    token = secrets.token_urlsafe(32)
    key = _hash(token)
    ttl = ttl_seconds()
    body = dict(payload)
    body["issued_at"] = datetime.now(timezone.utc).isoformat()

    if _use_memory_store():
        _prune_expired_memory()
        _MEMORY_TICKETS[key] = {
            "payload": body,
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=ttl),
        }
        logger.info("desktop_handoff_ticket_issued", ttl=ttl, store="memory")
        return token

    from core.infra.redis import get_redis
    r = get_redis()
    await r.set(f"{TICKET_KEY_PREFIX}{key}", json.dumps(body, ensure_ascii=False), ex=ttl)
    logger.info("desktop_handoff_ticket_issued", ttl=ttl)
    return token


async def consume_ticket(token: str) -> Optional[Dict[str, Any]]:
    """Atomically consume a ticket: delete on hit (single use). Returns the payload or None (invalid/expired/already used)."""
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
    # GETDEL atomically reads and deletes (redis ≥ 6.2); prevents concurrent replay
    try:
        raw = await r.getdel(full)
    except AttributeError:  # older clients lack getdel: degrade to get + delete
        raw = await r.get(full)
        if raw is not None:
            await r.delete(full)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("desktop_handoff_ticket_corrupt", token_hash=key[:8])
        return None
