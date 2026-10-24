"""Add, upsert, stream-refresh and delete chat messages."""
from __future__ import annotations
from core.infra.time import as_utc, utc_now
import uuid
from typing import Any, Dict, List, Optional
from core.db.models import ChatCompactionState, ChatMessage, ChatRun, ChatSession
from core.db.repository.chat import _strip_nul
from sqlalchemy import update
from core.ontology.revision import is_substantive_revision, normalize_revision_candidate
from core.chat import limits

def add_message(
    self,
    chat_id: str,
    role: str,
    content: str,
    model: Optional[str] = None,
    thinking: Optional[List[Dict[str, Any]]] = None,
    tool_calls: Optional[List[Dict]] = None,
    usage: Optional[Dict] = None,
    error: Optional[Dict] = None,
    extra_data: Dict = None,
    message_id: Optional[str] = None,
    commit: bool = True,
    chat_seq: Optional[int] = None,
    model_steps: Optional[List[Dict[str, Any]]] = None,
) -> ChatMessage:
    """Add a message to a chat session."""
    extra = dict(extra_data or {})
    kept_thinking, kept_segments = limits.clamp_thinking(thinking, extra.get("segments"))
    if "segments" in extra:
        extra["segments"] = kept_segments
    message_data = {
        "message_id": message_id or f"msg_{uuid.uuid4().hex[:16]}",
        "chat_id": chat_id,
        "chat_seq": chat_seq,
        "role": role,
        "content": limits.clamp_message_content(content),
        "model": model,
        "thinking": kept_thinking,
        "tool_calls": tool_calls,
        "model_steps": model_steps,
        "usage": usage,
        "error": error,
        "extra_data": extra,
    }

    message = self.message_repo.create(message_data, commit=commit)

    # Keep session metadata in sync for list APIs.
    session = self.session_repo.get_by_id(chat_id)
    if session:
        session.message_count = (session.message_count or 0) + 1
        now = utc_now()
        session.updated_at = now
        session.last_message_at = now
        if commit:
            self.db.commit()

    return message


def refresh_streaming_message(
    self,
    *,
    run_id: str,
    owner: str,
    message_id: str,
    content: str,
    thinking: Optional[List[Dict[str, Any]]],
    tool_calls: Optional[List[Dict]],
    extra_data: Dict[str, Any],
    model_steps: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """Overwrite the in-flight assistant row while its run streams.

    The run row lock serializes this write with its final commit, so a
    superseded or finishing worker cannot clobber the final message. Nothing else is touched — in particular no session
    timestamps, so pollers do not mistake a checkpoint for a new message.
    Returns False when the caller no longer owns the run.
    """
    # Lock the same run row as journal.complete before touching its message.
    # An EXISTS predicate alone can see a live lease, wait for the message
    # lock, and then overwrite a final commit using that stale MVCC view.
    live_run = (self.db.query(ChatRun)
                .filter(ChatRun.run_id == run_id, ChatRun.lease_owner == owner,
                        ChatRun.status.in_(("pending", "running")))
                .with_for_update().first())
    if live_run is None:
        return False
    extra = dict(extra_data)
    kept_thinking, kept_segments = limits.clamp_thinking(thinking, extra.get("segments"))
    if "segments" in extra:
        extra["segments"] = kept_segments
    result = self.db.execute(
        update(ChatMessage)
        .where(ChatMessage.message_id == message_id)
        .values(
            content=limits.clamp_message_content(content),
            thinking=_strip_nul(kept_thinking),
            tool_calls=_strip_nul(tool_calls),
            model_steps=_strip_nul(model_steps),
            extra_data=_strip_nul(extra),
        )
    )
    return result.rowcount > 0


def upsert_message(
    self,
    chat_id: str,
    role: str,
    content: str,
    *,
    message_id: str,
    model: Optional[str] = None,
    thinking: Optional[List[Dict[str, Any]]] = None,
    tool_calls: Optional[List[Dict]] = None,
    usage: Optional[Dict] = None,
    error: Optional[Dict] = None,
    extra_data: Dict = None,
    commit: bool = True,
    chat_seq: Optional[int] = None,
    model_steps: Optional[List[Dict[str, Any]]] = None,
    invalidate_mid_turn_checkpoint: bool = False,
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
        update: Dict[str, Any] = {"content": limits.clamp_message_content(content)}
        extra = dict(extra_data) if extra_data is not None else None
        kept_thinking, kept_segments = limits.clamp_thinking(
            thinking, (extra or {}).get("segments")
        )
        if model is not None:
            update["model"] = model
        if thinking is not None:
            update["thinking"] = kept_thinking
        if tool_calls is not None:
            update["tool_calls"] = tool_calls
        if model_steps is not None:
            update["model_steps"] = model_steps
        if usage is not None:
            update["usage"] = usage
        if error is not None:
            update["error"] = error
        if extra is not None:
            if "segments" in extra:
                extra["segments"] = kept_segments
            update["extra_data"] = extra
        if invalidate_mid_turn_checkpoint:
            # The completed turn has already crossed the post-turn trigger.
            # Its new steps cannot be safely sliced from a row covered by a
            # mid-turn checkpoint, so rebuild that successor from originals.
            # Match checkpoint publisher lock order: session, then lineage.
            self.db.query(ChatSession).filter(ChatSession.chat_id == chat_id).with_for_update().first()
            state = (self.db.query(ChatCompactionState)
                     .filter(ChatCompactionState.chat_id == chat_id)
                     .with_for_update().populate_existing().first())
            if (state is not None and state.active_checkpoint_id
                    and int(existing.chat_seq or 0) <= int(state.covered_seq or 0)):
                checkpoint = self.db.get(ChatMessage, state.active_checkpoint_id)
                manifest = ((checkpoint.extra_data or {}).get("replacement_manifest") or {}) if checkpoint else {}
                captured = (extra or {}).get("context_usage", {}).get("captured_at_ms", 0)
                checkpoint_ms = int(as_utc(checkpoint.created_at).timestamp() * 1000) if checkpoint else 0
                if (manifest.get("phase") == "mid_turn" and type(captured) is int
                        and captured > checkpoint_ms):
                    self._invalidate_compaction_lineage_for_rewrite(
                        chat_id, rewritten_seq=int(existing.chat_seq or 0)
                    )
        msg = self.message_repo.update(message_id, update, commit=commit)
        session = self.session_repo.get_by_id(chat_id)
        if session:
            now = utc_now()
            session.updated_at = now
            session.last_message_at = now
            if commit:
                self.db.commit()
        return msg or existing
    return self.add_message(
        chat_id=chat_id,
        role=role,
        content=content,
        model=model,
        thinking=thinking,
        tool_calls=tool_calls,
        usage=usage,
        error=error,
        extra_data=extra_data,
        message_id=message_id,
        commit=commit,
        chat_seq=chat_seq,
        model_steps=model_steps,
    )


def delete_messages_from(self, chat_id: str, message_id: str) -> int:
    """Delete a message and all subsequent messages in the chat.

    History rewrites invalidate the entire compaction lineage in the same
    transaction.  This fences an in-flight summarizer through the lineage
    version CAS and forces the next replay/compaction to rebuild from raw
    retained messages instead of trusting a stale covered watermark.

    Returns the number of messages deleted.
    """
    self._ensure_message_sequences(chat_id)
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

    self._invalidate_compaction_lineage_for_rewrite(
        chat_id, rewritten_seq=int(target.chat_seq)
    )

    deleted = (
        self.db.query(ChatMessage)
        .filter(
            ChatMessage.chat_id == chat_id,
            ChatMessage.chat_seq >= int(target.chat_seq),
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
        session.updated_at = utc_now()

    self.db.commit()
    return deleted


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
    # Some pure unit adapters exercise this method without a database.
    if not hasattr(self, "db"):
        return self.message_repo.update(
            message_id,
            {"content": candidate, "extra_data": extra_data},
        )

    updated = self.message_repo.update(
        message_id,
        {"content": candidate, "extra_data": extra_data},
        commit=False,
    )
    self._invalidate_compaction_lineage_for_rewrite(
        message.chat_id,
        rewritten_seq=int(message.chat_seq),
    )
    self.db.commit()
    return updated
