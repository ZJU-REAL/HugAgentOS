"""Chat id namespaces owned by a dedicated module surface, not the chat UI.

Such conversations are hidden from ordinary history and search and cannot be
renamed, deleted, forked, shared, rerun or reopened as an ordinary chat. The
namespaces are declared here rather than registered by their module: rows
already in the database must stay out of ordinary history even when the module
that wrote them is switched off.

Enforcement has two choke points; a new ordinary chat surface must go through one:
lookups by id resolve via ``ChatService.get_session`` / ``get_session_with_access``
(which treat reserved ids as nonexistent), and listings apply ``ordinary_chat_filter``.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import and_

# Prefix -> display name of the owning surface (used in error messages).
RESERVED_CHAT_NAMESPACES: dict[str, str] = {
    "pa_chat_": "Personal Agent",
}


def reserved_chat_owner(chat_id: Optional[str]) -> Optional[str]:
    """The owning surface's name when ``chat_id`` is reserved, else None."""
    for prefix, owner in RESERVED_CHAT_NAMESPACES.items():
        if str(chat_id or "").startswith(prefix):
            return owner
    return None


def ordinary_chat_filter(chat_id_column):
    """SQL clause keeping only chats outside every reserved namespace."""
    return and_(*(~chat_id_column.startswith(prefix) for prefix in RESERVED_CHAT_NAMESPACES))


def reserved_chat_error(chat_id: Optional[str]) -> Optional[str]:
    owner = reserved_chat_owner(chat_id)
    return f"This conversation is managed by {owner}" if owner else None
