"""Atomic, owner-scoped snapshots of a conversation's settled history."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from core.auth.permissions_iface import resolve_project_access
from core.db.models import ChatMessage, ChatRun, ChatSession, Project
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# These are conversation preferences, not task execution or sharing state.
SESSION_FIELDS = {
    "agent_id",
    "agent_name",
    "businessTopic",
    "chat_mode",
    "mode_slug",
    "model_name",
    "model_provider_id",
    "thinking",
    "thinking_effort",
}
MESSAGE_FIELDS = {
    "segments",
    "attachments",
    "artifacts",
    "workspace_files",
    "sources",
    "citations",
    "is_markdown",
    "warnings",
    "cancelled",
    "duration_ms",
    "quoted_follow_up",
    "referenced_chats",
    "_context_item",
    "hidden_in_chat",
    "skill_id",
    "skill_name",
    "plugin_id",
    "plugin_name",
    "connector_id",
    "connector_name",
    "mention_agent_id",
    "mention_name",
    "mention_source",
    "agent_source_profile",
    "invocation",
    "follow_up_questions",
    "forked_usage",
    "agent_id",
    "agent_profile",
    "mention_agent_profile",
    "model_provider_id",
    "plan_snapshot",
    "ontology_governance",
}
MESSAGE_REFERENCE_FIELDS = {
    "message_id",
    "source_message_id",
    "parent_message_id",
    "user_message_id",
    "assistant_message_id",
}


class ChatForkError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


def _remap_references(value, ids):
    if isinstance(value, list):
        return [_remap_references(item, ids) for item in value]
    if isinstance(value, dict):
        return {
            key: (
                ids.get(item, item)
                if key in MESSAGE_REFERENCE_FIELDS and isinstance(item, str)
                else _remap_references(item, ids)
            )
            for key, item in value.items()
        }
    return deepcopy(value)


def fork_message_metadata(source: dict, ids: dict) -> dict:
    result = _remap_references(
        {key: value for key, value in source.items() if key in MESSAGE_FIELDS}, ids
    )
    # Frontend treats inherited interactive cards as historical, never executable.
    result["forked_history"] = True
    return result


class ChatForkService:
    def __init__(self, db: Session):
        self.db = db

    def fork(
        self,
        source_id: str,
        user_id: str,
        request_id: UUID,
        through_message_id: str | None = None,
        title: str | None = None,
    ) -> ChatSession:
        destination_id = (
            "chat_" + uuid5(NAMESPACE_URL, f"hugagent/chat-fork/{user_id}/{request_id}").hex
        )
        fingerprint = json.dumps(
            [source_id, through_message_id, title], ensure_ascii=False, separators=(",", ":")
        )
        try:
            # First statement must acquire a write transaction on SQLite. On
            # Postgres this non-key UPDATE coordinates admission/history rewrites
            # without the child-FK blocking behavior of SELECT FOR UPDATE.
            locked = self.db.execute(
                update(ChatSession)
                .where(
                    ChatSession.chat_id == source_id,
                    ChatSession.user_id == user_id,
                    ChatSession.deleted_at.is_(None),
                )
                .values(
                    next_message_seq=ChatSession.next_message_seq, updated_at=ChatSession.updated_at
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            if not locked:
                raise ChatForkError(404, "chat_not_found", "会话不存在或无权创建分支")
            source = self.db.get(ChatSession, source_id)
            if source.project_id:
                project = self.db.get(Project, source.project_id)
                if resolve_project_access(self.db, user_id, project).level == "none":
                    raise ChatForkError(404, "project_not_found", "原聊天所属项目已不可访问")
            existing = self.db.get(ChatSession, destination_id)
            if existing is not None:
                result = self._retry(existing, fingerprint, user_id)
                self.db.commit()
                return result
            rows = self._prefix(source_id, through_message_id)
            ids = {row.message_id: "msg_" + uuid4().hex for row in rows}
            now = datetime.now(timezone.utc)
            metadata = {
                key: deepcopy(value)
                for key, value in (source.extra_data or {}).items()
                if key in SESSION_FIELDS
            }
            metadata.update(
                {
                    "title_manually_set": True,
                    "_fork_request": fingerprint,
                    "fork": {
                        "source_chat_id": source_id,
                        "source_message_id": rows[-1].message_id,
                        "source_chat_seq": rows[-1].chat_seq,
                        "source_title": source.title,
                        "message_count": len(rows),
                        "created_at": now.isoformat(),
                    },
                }
            )
            destination = ChatSession(
                chat_id=destination_id,
                user_id=user_id,
                title=title or (source.title[:493] + " · 分支"),
                project_id=source.project_id,
                extra_data=metadata,
                message_count=len(rows),
                next_message_seq=len(rows) + 1,
                created_at=now,
                updated_at=now,
                last_message_at=now,
                pinned=False,
                favorite=False,
                archived=False,
            )
            self.db.add(destination)
            self.db.flush()
            for seq, row in enumerate(rows, 1):
                extra = fork_message_metadata(row.extra_data or {}, ids)
                if row.usage is not None:
                    extra["forked_usage"] = deepcopy(row.usage)
                self.db.add(
                    ChatMessage(
                        chat_id=destination_id,
                        message_id=ids[row.message_id],
                        chat_seq=seq,
                        role=row.role,
                        content=row.content,
                        model=row.model,
                        thinking=deepcopy(row.thinking),
                        tool_calls=deepcopy(row.tool_calls),
                        model_steps=deepcopy(row.model_steps),
                        # Billing and usage reports aggregate the usage column.
                        # A snapshot preserves display information without a new charge.
                        usage=None,
                        error=deepcopy(row.error),
                        created_at=row.created_at,
                        extra_data=extra,
                    )
                )
            self.db.commit()
            return destination
        except IntegrityError:
            self.db.rollback()
            # Same request against different source locks can race on the target PK.
            winner = self.db.get(ChatSession, destination_id)
            if winner is None:
                raise
            try:
                return self._retry(winner, fingerprint, user_id)
            finally:
                self.db.rollback()
        except BaseException:
            self.db.rollback()
            raise

    @staticmethod
    def _retry(existing, fingerprint, user_id):
        if (
            existing.user_id != user_id
            or (existing.extra_data or {}).get("_fork_request") != fingerprint
        ):
            raise ChatForkError(409, "fork_request_conflict", "同一创建请求不能用于不同的分支参数")
        if existing.deleted_at is not None:
            raise ChatForkError(409, "fork_deleted", "该请求创建的分支已被删除，请重新创建")
        return existing

    def _prefix(self, source_id, through_message_id):
        # Snapshot all rows with a single SELECT. Do not copy checkpoints: their
        # summaries can contain facts beyond an earlier fork boundary.
        # Observe a running writer before reading its mutable rows. If it finishes
        # while this transaction reads history, the captured boundary stays safe.
        active = (
            self.db.query(ChatRun)
            .filter(
                ChatRun.chat_id == source_id,
                ChatRun.writer_slot == "main",
            )
            .first()
        )
        rows = (
            self.db.query(ChatMessage)
            .filter(
                ChatMessage.chat_id == source_id,
                ChatMessage.role != "system",
            )
            .order_by(ChatMessage.chat_seq)
            .all()
        )
        active_seq = None
        if active is not None:
            active_seq = active.user_chat_seq or active.assistant_chat_seq
            if active_seq is None:
                positions = [
                    r.chat_seq
                    for r in rows
                    if r.message_id in (active.message_id, active.user_message_id)
                ]
                active_seq = min(positions) if positions else 0
        candidates = [
            r
            for r in rows
            if r.role == "assistant" and (active_seq is None or r.chat_seq < active_seq)
        ]
        if through_message_id:
            target = next((r for r in rows if r.message_id == through_message_id), None)
            if target is None:
                raise ChatForkError(404, "message_not_found", "分叉位置不存在")
            if target not in candidates:
                raise ChatForkError(
                    409, "fork_boundary_unavailable", "请从已结束的助手回复创建分支"
                )
        else:
            target = candidates[-1] if candidates else None
        if target is None:
            raise ChatForkError(409, "fork_empty", "当前聊天还没有可创建分支的完整回复")
        return [r for r in rows if r.chat_seq <= target.chat_seq]
