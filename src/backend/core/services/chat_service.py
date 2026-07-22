"""Chat session and message business logic."""

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.auth.permissions_iface import ChatAccessLevel, can_delete_session, resolve_chat_access
from core.db.models import ChatMessage, ChatSession
from core.db.repository import AuditLogRepository, ChatMessageRepository, ChatSessionRepository
from core.ontology.revision import is_substantive_revision, normalize_revision_candidate
from sqlalchemy.orm import Session


class ChatService:
    """Service for chat-related operations."""

    def __init__(self, db: Session):
        self.db = db
        self.session_repo = ChatSessionRepository(db)
        self.message_repo = ChatMessageRepository(db)
        self.audit_repo = AuditLogRepository(db)

    def create_session(
        self,
        user_id: str,
        title: str = "新对话",
        extra_data: Dict = None,
        chat_id: Optional[str] = None,
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
        - Otherwise returns ``(session, 'admin'|'edit'|'read')``
        """
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

    def add_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        model: Optional[str] = None,
        tool_calls: Optional[List[Dict]] = None,
        usage: Optional[Dict] = None,
        error: Optional[Dict] = None,
        extra_data: Dict = None,
        message_id: Optional[str] = None,
    ) -> ChatMessage:
        """Add a message to a chat session."""
        message_data = {
            "message_id": message_id or f"msg_{uuid.uuid4().hex[:16]}",
            "chat_id": chat_id,
            "role": role,
            "content": content,
            "model": model,
            "tool_calls": tool_calls,
            "usage": usage,
            "error": error,
            "extra_data": extra_data or {},
        }

        message = self.message_repo.create(message_data)

        # Keep session metadata in sync for list APIs.
        session = self.session_repo.get_by_id(chat_id)
        if session:
            session.message_count = (session.message_count or 0) + 1
            now = datetime.utcnow()
            session.updated_at = now
            session.last_message_at = now
            self.db.commit()

        return message

    def upsert_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        *,
        message_id: str,
        tool_calls: Optional[List[Dict]] = None,
        usage: Optional[Dict] = None,
        extra_data: Dict = None,
    ) -> ChatMessage:
        """Idempotently upsert a message by message_id: overwrite in place if it exists, otherwise create.

        The autonomous loop uses this for "incrementally refreshing the same assistant message as
        progress advances" — each requirement processed / each evaluation round flushes the
        current accumulated body + tool cards into this message, so progress is visible in the DB
        even after a mid-run crash/refresh, instead of only being written once at the terminal
        state. The update path does not re-increment message_count (only +1 on creation).
        """
        existing = self.message_repo.get_by_id(message_id)
        if existing is not None:
            update: Dict[str, Any] = {"content": content}
            if tool_calls is not None:
                update["tool_calls"] = tool_calls
            if usage is not None:
                update["usage"] = usage
            if extra_data is not None:
                update["extra_data"] = extra_data
            msg = self.message_repo.update(message_id, update)
            session = self.session_repo.get_by_id(chat_id)
            if session:
                now = datetime.utcnow()
                session.updated_at = now
                session.last_message_at = now
                self.db.commit()
            return msg or existing
        return self.add_message(
            chat_id=chat_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            extra_data=extra_data,
            message_id=message_id,
        )

    def list_all_messages(self, chat_id: str, user_id: str) -> Optional[List[ChatMessage]]:
        """List all messages in chronological order with access check.

        Edition-specific sharing policies may also grant read access.

        **Excludes** compaction checkpoint rows (the only writer of role='system' in
        chat_messages is add_compaction_checkpoint) — internal artifacts, invisible to all
        downstream consumers (replay/export/sharing/title/memory), filtered at the SQL layer
        (a checkpoint's extra_data carries the entire replacement_history; loading it just to
        discard is pure waste). The replay layer (compaction_service) fetches them separately
        via get_latest_compaction_checkpoint.
        """
        pair = self.get_session_with_access(chat_id, user_id)
        if pair is None:
            return None

        return (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.chat_id == chat_id,
                ChatMessage.role != "system",
            )
            .order_by(ChatMessage.created_at)
            .all()
        )

    def list_messages(
        self, chat_id: str, user_id: str, page: int = 1, page_size: int = 50
    ) -> Optional[Tuple[List[ChatMessage], int, int]]:
        """List messages in a chat session."""
        # Check ownership
        session = self.get_session(chat_id, user_id)
        if not session:
            return None

        messages, total = self.message_repo.list_by_chat(chat_id, page, page_size)
        total_pages = (total + page_size - 1) // page_size

        return messages, total, total_pages

    def delete_messages_from(self, chat_id: str, message_id: str) -> int:
        """Delete a message and all subsequent messages in the chat.

        Returns the number of messages deleted.
        """
        target = (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.chat_id == chat_id,
                ChatMessage.message_id == message_id,
            )
            .first()
        )
        if not target:
            return 0

        deleted = (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.chat_id == chat_id,
                ChatMessage.created_at >= target.created_at,
            )
            .delete(synchronize_session="fetch")
        )

        # Update session message count
        session = self.session_repo.get_by_id(chat_id)
        if session:
            remaining = (
                self.db.query(ChatMessage)
                .filter(
                    ChatMessage.chat_id == chat_id,
                )
                .count()
            )
            session.message_count = remaining
            session.updated_at = datetime.utcnow()

        self.db.commit()
        return deleted

    def add_compaction_checkpoint(
        self,
        chat_id: str,
        *,
        summary_text: str,
        replacement_history: List[Dict],
    ) -> ChatMessage:
        """Write a compaction checkpoint (role='system').

        Persists the summary + replay history for later turns to consume directly without
        re-compacting. **Does not increment session.message_count** (invisible to the user), nor
        refresh last_message_at (not a real conversation turn).
        ``covers_up_to_*`` are troubleshooting breadcrumbs (replay cuts the tail by the
        checkpoint row's own created_at); computed here from the last message in place, callers
        need not pass them.
        """
        from core.llm.compaction import COMPACTION_CHECKPOINT_KIND

        last = (
            self.db.query(ChatMessage.message_id, ChatMessage.created_at)
            .filter(ChatMessage.chat_id == chat_id)
            .order_by(ChatMessage.created_at.desc())
            .first()
        )

        message = self.message_repo.create(
            {
                "message_id": f"cmpct_{uuid.uuid4().hex[:16]}",
                "chat_id": chat_id,
                "role": "system",
                "content": summary_text,
                "extra_data": {
                    "kind": COMPACTION_CHECKPOINT_KIND,
                    "replacement_history": replacement_history,
                    "covers_up_to_message_id": last.message_id if last else None,
                    "covers_up_to_created_at": (
                        last.created_at.isoformat() if last and last.created_at else None
                    ),
                    # Pending-notice flag: consumed by the executor on the next turn's first frame
                    # (pop_compaction_notice) → emits a compaction_notice SSE event to tell the user
                    # compaction happened.
                    "notice_pending": True,
                },
            }
        )
        self.db.commit()
        return message

    def get_latest_compaction_checkpoint(self, chat_id: str) -> Optional[ChatMessage]:
        """Return the chat's latest compaction checkpoint (None if there is none).

        The kind filter is done on the Python side to avoid depending on Postgres-specific
        ``->>`` (SQLite compatible). Each checkpoint's extra_data carries the entire
        replacement_history (can reach ~100KB), and rolling compaction keeps accumulating —
        scan only the latest few rows, never ``.all()`` load everything.
        """
        from core.llm.compaction import COMPACTION_CHECKPOINT_KIND

        rows = (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.chat_id == chat_id,
                ChatMessage.role == "system",
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(5)
            .all()
        )
        for r in rows:
            if (r.extra_data or {}).get("kind") == COMPACTION_CHECKPOINT_KIND:
                return r
        return None

    def get_message_by_id(self, message_id: str) -> Optional[ChatMessage]:
        """Get a single message by its ID."""
        return (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.message_id == message_id,
            )
            .first()
        )

    def get_message_by_index(self, chat_id: str, index: int) -> Optional[ChatMessage]:
        """Get a message by its position (0-based) in the chat, ordered by created_at."""
        return (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.chat_id == chat_id,
            )
            .order_by(ChatMessage.created_at)
            .offset(index)
            .limit(1)
            .first()
        )

    def get_user_message_before(self, chat_id: str, message_id: str) -> Optional[ChatMessage]:
        """Get the user message immediately before the given message."""
        target = self.get_message_by_id(message_id)
        if not target:
            return None
        return (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.chat_id == chat_id,
                ChatMessage.role == "user",
                ChatMessage.created_at < target.created_at,
            )
            .order_by(ChatMessage.created_at.desc())
            .first()
        )

    def update_message_extra_data(
        self,
        message_id: str,
        patch: Dict[str, Any],
    ) -> bool:
        """Merge *patch* into a message's extra_data. Returns True on success."""
        return self.message_repo.update_extra_data(message_id, patch) is not None

    def accept_ontology_revision(self, message_id: str) -> Optional[ChatMessage]:
        """Replace an assistant message with its persisted ontology revision candidate."""
        message = self.message_repo.get_by_id(message_id)
        if not message or message.role != "assistant":
            return None
        extra_data = dict(message.extra_data or {})
        governance = extra_data.get("ontology_governance")
        if not isinstance(governance, dict):
            return None
        review = governance.get("review")
        if not isinstance(review, dict):
            return None
        candidate = normalize_revision_candidate(review.get("candidate_answer"))
        if not is_substantive_revision(candidate):
            return None
        updated_review = {**review, "candidate_answer": candidate, "accepted": True}
        extra_data["ontology_governance"] = {**governance, "review": updated_review}
        return self.message_repo.update(
            message_id,
            {"content": candidate, "extra_data": extra_data},
        )

    def search_sessions(
        self,
        user_id: str,
        query: str,
        page: int = 1,
        page_size: int = 20,
        scope: str = "title",
    ) -> Tuple[list, int]:
        """Search chat sessions by title (and optionally message content)."""
        results, total = self.session_repo.search(user_id, query, page, page_size, scope=scope)
        return results, total
