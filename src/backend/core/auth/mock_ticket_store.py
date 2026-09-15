"""One-time ticket store for local-account and mock-SSO login.

Holds the one-time ticket state used by the local-account and mock SSO flows.
Relocated out of ``api/routes/v1/mock_sso.py`` so that edition authentication can
validate tickets without importing an API route module (breaks the
``core/auth → api`` upward dependency). The unified login route and mock SSO
route both reuse this store.

The ticket is issued by ``POST /login`` and redeemed by a *separate* request —
the browser follows the redirect and the frontend posts the ticket back to
``/v1/auth/ticket/exchange``. Nothing routes those two requests to the same
uvicorn worker, so the state between them cannot live in one process: with
``WEB_CONCURRENCY > 1`` an in-process dict loses roughly ``1 - 1/N`` of all
logins. It is therefore kept in Redis, with the same key/TTL discipline as
:mod:`core.auth.oa_ticket_store` and :mod:`core.auth.desktop_ticket_store`:
sha256 of the token as the key, atomic GETDEL on consume, TTL-bounded. Without
Redis the backend degrades to in-process storage, which is sound because
:mod:`core.infra.worker_count` already clamps such a deployment to one worker.
"""

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from core.config.settings import settings
from core.infra.logging import get_logger

logger = get_logger(__name__)

TICKET_KEY_PREFIX = "jx:login_ticket:"
TICKET_TTL = 300  # seconds
TICKET_PREFIX = "mock_ticket_"

_MEMORY_TICKETS: Dict[str, Dict[str, Any]] = {}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _use_memory_store() -> bool:
    return settings.session.store_type == "memory"


def _prune_expired_memory() -> None:
    now = datetime.now(timezone.utc)
    for key in [k for k, v in _MEMORY_TICKETS.items() if v["expires_at"] <= now]:
        _MEMORY_TICKETS.pop(key, None)


def is_local_ticket(ticket: str) -> bool:
    """Return whether a credential belongs to this login ticket namespace."""
    return bool(ticket) and ticket.startswith(TICKET_PREFIX)


async def generate_ticket(user_info: Dict[str, Any]) -> str:
    """Issue a one-time ticket for the given user; returns the raw token."""
    token = f"{TICKET_PREFIX}{secrets.token_urlsafe(16)}"
    key = _hash(token)

    if _use_memory_store():
        _prune_expired_memory()
        _MEMORY_TICKETS[key] = {
            "user_info": dict(user_info),
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=TICKET_TTL),
        }
        return token

    from core.infra.redis import get_redis

    r = get_redis()
    await r.set(
        f"{TICKET_KEY_PREFIX}{key}",
        json.dumps(user_info, ensure_ascii=False, default=str),
        ex=TICKET_TTL,
    )
    return token


async def consume_ticket(ticket: str) -> Optional[Dict[str, Any]]:
    """Atomically consume a ticket (single use). Returns user info or None."""
    if not ticket:
        return None
    key = _hash(ticket)

    if _use_memory_store():
        _prune_expired_memory()
        entry = _MEMORY_TICKETS.pop(key, None)
        return dict(entry["user_info"]) if entry else None

    from core.infra.redis import get_redis

    r = get_redis()
    full = f"{TICKET_KEY_PREFIX}{key}"
    try:
        raw = await r.getdel(full)
    except AttributeError:  # older clients lack getdel
        raw = await r.get(full)
        if raw is not None:
            await r.delete(full)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("login_ticket_corrupt", token_hash=key[:8])
        return None
