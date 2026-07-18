"""Data access layer — chat repositories.

Split out of the former monolithic ``core/db/repository.py``. The package
``__init__`` re-exports every repository class, so ``from core.db.repository
import XxxRepository`` keeps working unchanged.
"""

import logging
from typing import Optional, List, Dict, Any
from datetime import datetime
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, desc, func, select
from core.db.models import (
    UserShadow, ChatSession, ChatMessage, CatalogOverride,
    KBSpace, KBDocument, Artifact, AuditLog, UserAgent,
    LocalUser, Team, TeamMember, TeamFolder, InviteCode,
)


class ChatSessionRepository:
    """Repository for chat session operations."""

    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, chat_id: str) -> Optional[ChatSession]:
        """Get chat session by ID."""
        return self.db.query(ChatSession).filter(
            ChatSession.chat_id == chat_id,
            ChatSession.deleted_at.is_(None)
        ).first()

    def list_by_user(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
        pinned_only: bool = False,
        favorite_only: bool = False,
        exclude_automation: bool = False,
    ) -> tuple[List[ChatSession], int]:
        """List chat sessions for a user with pagination."""
        query = self.db.query(ChatSession).filter(
            ChatSession.user_id == user_id,
            ChatSession.deleted_at.is_(None)
        )

        if pinned_only:
            query = query.filter(ChatSession.pinned == True)
        if favorite_only:
            query = query.filter(ChatSession.favorite == True)
        if exclude_automation:
            # Exclude sessions created by automation scheduler.
            # extra_data is mapped to the "metadata" JSON column.
            # Use dialect-portable cast: check the JSON text doesn't contain the marker key.
            query = query.filter(
                or_(
                    ChatSession.extra_data.is_(None),
                    ~func.cast(ChatSession.extra_data, sa.Text).contains('"automation_run"'),
                )
            )

        # Get total count
        total = query.count()

        # Apply pagination and ordering
        sessions = query.order_by(desc(ChatSession.updated_at)).offset(
            (page - 1) * page_size
        ).limit(page_size).all()

        return sessions, total

    def create(self, session_data: Dict[str, Any]) -> ChatSession:
        """Create a new chat session."""
        session = ChatSession(**session_data)
        session.created_at = datetime.utcnow()
        session.updated_at = datetime.utcnow()
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    def update(self, chat_id: str, update_data: Dict[str, Any]) -> Optional[ChatSession]:
        """Update chat session."""
        session = self.get_by_id(chat_id)
        if not session:
            return None

        for key, value in update_data.items():
            setattr(session, key, value)

        session.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(session)
        return session

    def soft_delete(self, chat_id: str) -> bool:
        """Soft delete a chat session."""
        session = self.get_by_id(chat_id)
        if not session:
            return False

        session.deleted_at = datetime.utcnow()
        self.db.commit()
        return True

    def search(
        self, user_id: str, query: str, page: int = 1, page_size: int = 20, scope: str = "title"
    ) -> tuple[List[Dict[str, Any]], int]:
        """Search chat sessions by title and optionally message content.

        Args:
            scope: "title" (default) searches title only;
                   "all" searches both title and message content.

        Returns:
            A list of dicts with ChatSession + match_type + matched_snippet, and total count.
            Results are ordered: title matches first (by updated_at desc),
            then content-only matches (by updated_at desc).
        """
        like_pattern = f"%{query}%"

        base_filter = and_(
            ChatSession.user_id == user_id,
            ChatSession.deleted_at.is_(None),
        )

        # Title-matching chat_ids (always needed)
        title_id_set: set[str] = {
            row[0]
            for row in self.db.query(ChatSession.chat_id)
            .filter(base_filter, ChatSession.title.ilike(like_pattern))
            .all()
        }

        if scope == "all":
            # Content-only matching chat_ids (exclude ones already matched by title)
            content_id_set: set[str] = set()
            content_rows = (
                self.db.query(ChatMessage.chat_id)
                .join(ChatSession, ChatMessage.chat_id == ChatSession.chat_id)
                .filter(
                    base_filter,
                    ChatMessage.role.in_(["user", "assistant"]),
                    ChatMessage.content.ilike(like_pattern),
                )
                .distinct()
                .all()
            )
            content_id_set = {row[0] for row in content_rows} - title_id_set
            all_ids = title_id_set | content_id_set
        else:
            content_id_set = set()
            all_ids = title_id_set

        total = len(all_ids)

        # Fetch title-matched sessions first, then content-matched sessions
        title_sessions = (
            self.db.query(ChatSession)
            .filter(ChatSession.chat_id.in_(title_id_set))
            .order_by(desc(ChatSession.updated_at))
            .all()
        ) if title_id_set else []

        content_sessions = (
            self.db.query(ChatSession)
            .filter(ChatSession.chat_id.in_(content_id_set))
            .order_by(desc(ChatSession.updated_at))
            .all()
        ) if content_id_set else []

        # Merge: title matches first, then content matches
        ordered = title_sessions + content_sessions

        # Apply pagination on the merged list
        start = (page - 1) * page_size
        page_sessions = ordered[start : start + page_size]

        results: List[Dict[str, Any]] = []
        for s in page_sessions:
            match_type = "title" if s.chat_id in title_id_set else "content"
            matched_snippet: Optional[str] = None

            if match_type == "content":
                msg = (
                    self.db.query(ChatMessage)
                    .filter(
                        ChatMessage.chat_id == s.chat_id,
                        ChatMessage.role.in_(["user", "assistant"]),
                        ChatMessage.content.ilike(like_pattern),
                    )
                    .order_by(ChatMessage.created_at)
                    .first()
                )
                if msg and msg.content:
                    # Center the snippet around the keyword
                    content = msg.content.replace("\n", " ")
                    lower_content = content.lower()
                    idx = lower_content.find(query.lower())
                    if idx == -1:
                        matched_snippet = content[:30]
                    else:
                        snippet_len = 30
                        half = snippet_len // 2
                        start_pos = max(0, idx - half)
                        end_pos = min(len(content), start_pos + snippet_len)
                        snippet = content[start_pos:end_pos]
                        if start_pos > 0:
                            snippet = "..." + snippet
                        if end_pos < len(content):
                            snippet = snippet + "..."
                        matched_snippet = snippet

            results.append({
                "session": s,
                "match_type": match_type,
                "matched_snippet": matched_snippet,
            })

        return results, total


class ChatMessageRepository:
    """Repository for chat message operations."""

    def __init__(self, db: Session):
        self.db = db

    def get_by_id(self, message_id: str) -> Optional[ChatMessage]:
        """Get message by ID."""
        return self.db.query(ChatMessage).filter(
            ChatMessage.message_id == message_id
        ).first()

    def list_by_chat(
        self,
        chat_id: str,
        page: int = 1,
        page_size: int = 50
    ) -> tuple[List[ChatMessage], int]:
        """List messages for a chat session with pagination.

        Excludes role='system' rows — these include compaction checkpoints (internal
        artifacts, not visible to the user).
        """
        query = self.db.query(ChatMessage).filter(
            ChatMessage.chat_id == chat_id,
            ChatMessage.role != "system",
        )

        total = query.count()
        messages = query.order_by(ChatMessage.created_at).offset(
            (page - 1) * page_size
        ).limit(page_size).all()

        return messages, total

    def create(self, message_data: Dict[str, Any]) -> ChatMessage:
        """Create a new chat message."""
        message = ChatMessage(**message_data)
        message.created_at = datetime.utcnow()
        self.db.add(message)
        self.db.commit()
        self.db.refresh(message)
        return message

    def update(self, message_id: str, update_data: Dict[str, Any]) -> Optional[ChatMessage]:
        """Update mutable fields (content / tool_calls / usage / extra_data / …) in place.

        Used for scenarios like the autonomous loop where "the same assistant message is
        incrementally refreshed as progress advances" — overwrites in place by message_id,
        without adding a new row. Returns None for an unknown message_id.
        """
        message = self.get_by_id(message_id)
        if not message:
            return None
        for key, value in update_data.items():
            setattr(message, key, value)
        self.db.commit()
        self.db.refresh(message)
        return message

    def update_extra_data(self, message_id: str, patch: Dict[str, Any]) -> Optional[ChatMessage]:
        """Merge *patch* into the message's extra_data JSONB field."""
        message = self.get_by_id(message_id)
        if not message:
            return None
        merged = dict(message.extra_data or {})
        merged.update(patch)
        message.extra_data = merged
        self.db.commit()
        self.db.refresh(message)
        return message

