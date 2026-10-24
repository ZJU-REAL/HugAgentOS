"""Create, list, update and delete chat sessions, with ownership and sharing checks."""
from __future__ import annotations
import uuid
from typing import Any, Dict, List, Optional, Tuple
from core.auth.edition import ChatAccessLevel, resolve_chat_access
from core.db.models import ChatSession
from core.db.reserved_chats import reserved_chat_owner

def create_session(
    self,
    user_id: str,
    title: str = "新对话",
    extra_data: Dict = None,
    chat_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> ChatSession:
    """Create a new chat session.

    If `chat_id` is provided and belongs to the same user, reuse it.
    If it belongs to another user, generate a new chat_id.
    """
    if chat_id:
        existing = self.session_repo.get_by_id(chat_id)
        if existing:
            if existing.user_id == user_id:
                return existing
            chat_id = None

    session_data = {
        "chat_id": chat_id or f"chat_{uuid.uuid4().hex[:16]}",
        "user_id": user_id,
        "title": title,
        "extra_data": extra_data or {},
        "project_id": project_id,
    }
    session = self.session_repo.create(session_data)

    # Audit log
    self.audit_repo.create(
        {
            "user_id": user_id,
            "action": "chat.session.created",
            "resource_type": "chat_session",
            "resource_id": session.chat_id,
            "status": "success",
        }
    )

    return session


def ensure_session(
    self,
    chat_id: str,
    user_id: str,
    title: str = "新对话",
    extra_data: Optional[Dict] = None,
    project_id: Optional[str] = None,
) -> Optional[ChatSession]:
    """Ensure a chat session exists for user and chat_id.

    When ``project_id`` is given, attach the session to that project (first write wins; if
    already attached to a different project it stays unchanged — in-project chats never drift
    across projects). All session-creation entry points share this attachment rule.
    """
    existing = self.session_repo.get_by_id(chat_id)
    if existing:
        if existing.user_id != user_id:
            return None
        # Merge any missing metadata flags into existing session
        if extra_data:
            merged = dict(existing.extra_data or {})
            changed = False
            for k, v in extra_data.items():
                if k not in merged:
                    merged[k] = v
                    changed = True
            if changed:
                existing.extra_data = merged
                self.db.commit()
        session = existing
    else:
        session = self.create_session(
            user_id=user_id,
            title=title,
            extra_data=extra_data or {},
            chat_id=chat_id,
            project_id=project_id,
        )
    if session is not None and project_id and not session.project_id:
        session.project_id = project_id
        self.db.commit()
    return session


def list_sessions(
    self,
    user_id: str,
    page: int = 1,
    page_size: int = 20,
    pinned_only: bool = False,
    favorite_only: bool = False,
    exclude_automation: bool = False,
) -> Tuple[List[ChatSession], int, int]:
    """List chat sessions with pagination."""
    sessions, total = self.session_repo.list_by_user(
        user_id,
        page,
        page_size,
        pinned_only,
        favorite_only,
        exclude_automation=exclude_automation,
    )

    total_pages = (total + page_size - 1) // page_size

    return sessions, total, total_pages


def get_session(self, chat_id: str, user_id: str) -> Optional[ChatSession]:
    """Get chat session with ownership check.

    Historical semantics unchanged: only the owner can get the session. For
    edition-specific sharing, use :py:meth:`get_session_with_access`.
    """
    # Module-owned chats are reachable only through their module, never as ordinary chats.
    if reserved_chat_owner(chat_id):
        return None
    session = self.session_repo.get_by_id(chat_id)

    if session and session.user_id != user_id:
        # Access denied - user doesn't own this session
        return None

    return session


def get_session_with_access(
    self, chat_id: str, user_id: str
) -> Optional[Tuple[ChatSession, ChatAccessLevel]]:
    """Get a session in a sharing context + compute the access level.

    - Does not exist / soft-deleted → ``None``
    - No access (``resolve_chat_access`` returns ``'none'``) → ``None``
    - Module-owned (reserved namespace) → ``None``: not an ordinary chat
    - Otherwise returns ``(session, 'admin'|'edit'|'read')``
    """
    if reserved_chat_owner(chat_id):
        return None
    session = self.session_repo.get_by_id(chat_id)
    if session is None:
        return None
    level = resolve_chat_access(self.db, user_id, session)
    if level == "none":
        return None
    return session, level


def update_session_fields(
    self,
    chat_id: str,
    fields: Dict[str, Any],
    *,
    actor_user_id: Optional[str] = None,
) -> Optional[ChatSession]:
    """Field update without ownership check (for sharing contexts).

    The caller is responsible for permission checks beforehand. ``actor_user_id`` is used
    only for the audit log. The ``extra_data`` field is merged rather than overwritten.
    """
    session = self.session_repo.get_by_id(chat_id)
    if session is None:
        return None
    normalized = dict(fields)
    extra_patch = normalized.get("extra_data")
    if isinstance(extra_patch, dict):
        merged = dict(session.extra_data or {})
        merged.update(extra_patch)
        normalized["extra_data"] = merged
    updated = self.session_repo.update(chat_id, normalized)
    self.audit_repo.create(
        {
            "user_id": actor_user_id or session.user_id,
            "action": "chat.session.updated",
            "resource_type": "chat_session",
            "resource_id": chat_id,
            "details": normalized,
            "status": "success",
        }
    )
    return updated


def delete_session_force(self, chat_id: str, *, actor_user_id: str) -> bool:
    """Forced delete in a sharing context (no ownership check). Caller handles permissions."""
    session = self.session_repo.get_by_id(chat_id)
    if session is None:
        return False
    result = self.session_repo.soft_delete(chat_id)
    if result:
        self.audit_repo.create(
            {
                "user_id": actor_user_id,
                "action": "chat.session.deleted",
                "resource_type": "chat_session",
                "resource_id": chat_id,
                "details": {
                    "owner_user_id": session.user_id,
                    "deleted_by_owner": session.user_id == actor_user_id,
                },
                "status": "success",
            }
        )
    return result


def update_session(
    self, chat_id: str, user_id: str, update_data: Dict[str, Any]
) -> Optional[ChatSession]:
    """Update chat session."""
    session = self.get_session(chat_id, user_id)
    if not session:
        return None

    normalized_update_data = dict(update_data)
    extra_data_patch = normalized_update_data.get("extra_data")
    if isinstance(extra_data_patch, dict):
        merged_extra_data = dict(session.extra_data or {})
        merged_extra_data.update(extra_data_patch)
        normalized_update_data["extra_data"] = merged_extra_data

    updated_session = self.session_repo.update(chat_id, normalized_update_data)

    # Audit log
    self.audit_repo.create(
        {
            "user_id": user_id,
            "action": "chat.session.updated",
            "resource_type": "chat_session",
            "resource_id": chat_id,
            "details": normalized_update_data,
            "status": "success",
        }
    )

    return updated_session


def delete_session(self, chat_id: str, user_id: str) -> bool:
    """Delete chat session (soft delete)."""
    session = self.get_session(chat_id, user_id)
    if not session:
        return False

    result = self.session_repo.soft_delete(chat_id)

    if result:
        # Audit log
        self.audit_repo.create(
            {
                "user_id": user_id,
                "action": "chat.session.deleted",
                "resource_type": "chat_session",
                "resource_id": chat_id,
                "status": "success",
            }
        )

    return result


def search_sessions(
    self,
    user_id: str,
    query: str,
    page: int = 1,
    page_size: int = 20,
    scope: str = "title",
) -> Tuple[list, int]:
    """Search chat sessions by title (and optionally message content)."""
    results, total = self.session_repo.search(
        user_id, query, page, page_size, scope=scope
    )
    return results, total
