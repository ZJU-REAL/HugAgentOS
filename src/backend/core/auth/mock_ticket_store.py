"""In-process ticket store for local-account and mock-SSO login.

Holds the one-time ticket state used by the local-account and mock SSO flows.
Relocated out of ``api/routes/v1/mock_sso.py`` so that edition authentication can
validate tickets without importing an API route module (breaks the
``core/auth → api`` upward dependency). The unified login route and mock SSO
route both reuse this store.
"""

import secrets
import time
from typing import Any, Dict, Optional

# ticket → { user_info, created_at }. Tickets expire after TICKET_TTL seconds
# and are one-time use.
TICKET_STORE: Dict[str, Dict[str, Any]] = {}
TICKET_TTL = 300  # seconds
TICKET_PREFIX = "mock_ticket_"


def cleanup_expired() -> None:
    """Remove expired tickets from the store."""
    now = time.time()
    expired = [t for t, v in TICKET_STORE.items() if now - v["created_at"] > TICKET_TTL]
    for t in expired:
        TICKET_STORE.pop(t, None)


def generate_ticket(user_info: Dict[str, Any]) -> str:
    """Generate a one-time ticket for the given user."""
    cleanup_expired()
    ticket = f"{TICKET_PREFIX}{secrets.token_urlsafe(16)}"
    TICKET_STORE[ticket] = {
        "user_info": user_info,
        "created_at": time.time(),
    }
    return ticket


def is_local_ticket(ticket: str) -> bool:
    """Return whether a credential belongs to this process-local ticket namespace."""
    return bool(ticket) and ticket.startswith(TICKET_PREFIX)


def consume_ticket(ticket: str) -> Optional[Dict[str, Any]]:
    """Consume (validate + delete) a ticket. Returns user info or None."""
    cleanup_expired()
    entry = TICKET_STORE.pop(ticket, None)
    if entry is None:
        return None
    if time.time() - entry["created_at"] > TICKET_TTL:
        return None
    return entry["user_info"]
