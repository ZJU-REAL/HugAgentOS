"""Transactional per-chat sequencing and main-run admission.

This module is the only public Seam that may accept an interactive main run.
It commits the user message and pending ChatRun together, so a busy response
cannot leave an orphan message and a worker can never start before acceptance
is durable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.chat import inflight
from core.db.models import ChatMessage, ChatRun, ChatSession
from core.db.models.chat import reserve_chat_sequences
from core.services.chat_service import clamp_message_content

LIVE_RUN_STATUSES = ("pending", "running")
TERMINAL_RUN_STATUSES = ("completed", "failed", "cancelled")
MAIN_WRITER_SLOT = "main"


@dataclass(frozen=True)
class ActiveRunRef:
    run_id: str
    message_id: str
    status: str


@dataclass(frozen=True)
class AcceptedChatRun:
    run: ChatRun
    user_message: ChatMessage


class ChatBusyError(RuntimeError):
    """The chat already has a durable main writer."""

    def __init__(self, active_run: ActiveRunRef):
        self.active_run = active_run
        super().__init__(f"chat already has active run {active_run.run_id}")


class ChatSequencer:
    """Allocate chat order and accept/release the single main writer."""

    def __init__(self, db: Session):
        self.db = db

    def _recount(self, chat_id: str, now: datetime, *, touch_last_message: bool = False) -> None:
        """Bring ``message_count`` in line with the rows this transaction just wrote."""
        self.db.flush()
        remaining = (
            self.db.query(func.count(ChatMessage.message_id))
            .filter(ChatMessage.chat_id == chat_id)
            .scalar()
        )
        values: Dict[str, Any] = {"message_count": remaining, "updated_at": now}
        if touch_last_message:
            values["last_message_at"] = now
        self.db.execute(update(ChatSession).where(ChatSession.chat_id == chat_id).values(**values))

    def get_active_run(self, chat_id: str) -> Optional[ActiveRunRef]:
        run = (
            self.db.query(ChatRun)
            .filter(
                ChatRun.chat_id == chat_id,
                ChatRun.writer_slot == MAIN_WRITER_SLOT,
            )
            .order_by(ChatRun.created_at.desc(), ChatRun.run_id.desc())
            .first()
        )
        if run is None:
            return None
        return ActiveRunRef(run_id=run.run_id, message_id=run.message_id, status=run.status)

    def accept_main_run(
        self,
        *,
        chat_id: str,
        user_id: str,
        user_content: str,
        request_payload: Dict[str, Any],
        model: Optional[str] = None,
        user_extra_data: Optional[Dict[str, Any]] = None,
    ) -> AcceptedChatRun:
        """Commit the user input and pending run in one transaction.

        Correctness comes from the database unique constraint: a concurrent
        loser rolls its message and sequence reservation back before observing
        the winner.  The atomic allocator UPDATE is intentionally the first DB
        statement so the SQLite profile has the same admission semantics.
        """

        now = datetime.now(timezone.utc)
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        user_message_id = f"msg_{uuid.uuid4().hex[:16]}"
        assistant_message_id = f"msg_{uuid.uuid4().hex[:16]}"

        try:
            # This atomic UPDATE is deliberately the first database statement:
            # it serializes concurrent admission even on the SQLite profile.
            first_seq = reserve_chat_sequences(
                self.db,
                chat_id,
                count=2,
                owner_user_id=user_id,
            )
            user_message = ChatMessage(
                message_id=user_message_id,
                chat_id=chat_id,
                chat_seq=first_seq,
                role="user",
                content=clamp_message_content(user_content),
                model=model,
                extra_data=dict(user_extra_data or {}),
                created_at=now,
            )
            run = ChatRun(
                run_id=run_id,
                chat_id=chat_id,
                user_id=user_id,
                message_id=assistant_message_id,
                user_message_id=user_message_id,
                user_chat_seq=first_seq,
                assistant_chat_seq=first_seq + 1,
                writer_slot=MAIN_WRITER_SLOT,
                status="pending",
                request_payload=dict(request_payload or {}),
                last_event_offset=0,
                created_at=now,
            )
            assistant_message = inflight.new_assistant_row(
                message_id=assistant_message_id,
                chat_id=chat_id,
                chat_seq=first_seq + 1,
                run_id=run_id,
                model=model,
                created_at=now,
            )
            self.db.add_all((user_message, assistant_message, run))
            self._recount(chat_id, now, touch_last_message=True)
            self.db.commit()
            self.db.refresh(user_message)
            self.db.refresh(run)
            return AcceptedChatRun(run=run, user_message=user_message)
        except IntegrityError as exc:
            self.db.rollback()
            winner = self.get_active_run(chat_id)
            if winner is not None:
                raise ChatBusyError(winner) from exc
            raise
        except Exception:
            self.db.rollback()
            raise

    def accept_existing_user_run(
        self,
        *,
        chat_id: str,
        user_id: str,
        user_message_id: str,
        request_payload: Dict[str, Any],
        delete_from_message_id: Optional[str] = None,
        delete_from_chat_seq: Optional[int] = None,
    ) -> AcceptedChatRun:
        """Accept a run anchored to an existing user message.

        When ``delete_from_chat_seq`` is supplied, tail deletion is part of the
        same admission transaction.  A competing writer therefore rolls back
        both the deletion and the sequence reservation before returning busy.
        """

        now = datetime.now(timezone.utc)
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        assistant_message_id = f"msg_{uuid.uuid4().hex[:16]}"
        try:
            assistant_seq = reserve_chat_sequences(
                self.db,
                chat_id,
                owner_user_id=user_id,
            )
            user_message = self.db.get(ChatMessage, user_message_id)
            if (
                user_message is None
                or user_message.chat_id != chat_id
                or user_message.role != "user"
            ):
                raise ValueError("existing user message is not a valid run anchor")
            if delete_from_chat_seq is not None:
                boundary = (
                    self.db.get(ChatMessage, delete_from_message_id)
                    if delete_from_message_id
                    else None
                )
                if (
                    boundary is None
                    or boundary.chat_id != chat_id
                    or boundary.chat_seq != delete_from_chat_seq
                ):
                    raise ValueError("rewrite boundary is no longer valid")
                if delete_from_chat_seq <= user_message.chat_seq:
                    raise ValueError("rewrite boundary must follow the user anchor")
                deleted = (
                    self.db.query(ChatMessage)
                    .filter(
                        ChatMessage.chat_id == chat_id,
                        ChatMessage.chat_seq >= delete_from_chat_seq,
                    )
                    .delete(synchronize_session=False)
                )
                if not deleted:
                    raise ValueError("rewrite boundary no longer exists")
            run = ChatRun(
                run_id=run_id,
                chat_id=chat_id,
                user_id=user_id,
                message_id=assistant_message_id,
                user_message_id=user_message.message_id,
                user_chat_seq=user_message.chat_seq,
                assistant_chat_seq=assistant_seq,
                writer_slot=MAIN_WRITER_SLOT,
                status="pending",
                request_payload=dict(request_payload or {}),
                last_event_offset=0,
                created_at=now,
            )
            self.db.add_all(
                (
                    run,
                    inflight.new_assistant_row(
                        message_id=assistant_message_id,
                        chat_id=chat_id,
                        chat_seq=assistant_seq,
                        run_id=run_id,
                        model=user_message.model,
                        created_at=now,
                    ),
                )
            )
            self._recount(chat_id, now)
            self.db.commit()
            self.db.refresh(run)
            return AcceptedChatRun(run=run, user_message=user_message)
        except IntegrityError as exc:
            self.db.rollback()
            winner = self.get_active_run(chat_id)
            if winner is not None:
                raise ChatBusyError(winner) from exc
            raise
        except Exception:
            self.db.rollback()
            raise

    def accept_replacement_user_run(
        self,
        *,
        chat_id: str,
        user_id: str,
        target_user_message_id: str,
        delete_from_chat_seq: int,
        user_content: str,
        request_payload: Dict[str, Any],
        model: Optional[str] = None,
        user_extra_data: Optional[Dict[str, Any]] = None,
    ) -> AcceptedChatRun:
        """Atomically replace a user turn, delete its tail, and accept a run."""

        now = datetime.now(timezone.utc)
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        user_message_id = f"msg_{uuid.uuid4().hex[:16]}"
        assistant_message_id = f"msg_{uuid.uuid4().hex[:16]}"
        try:
            first_seq = reserve_chat_sequences(
                self.db,
                chat_id,
                count=2,
                owner_user_id=user_id,
            )
            target = self.db.get(ChatMessage, target_user_message_id)
            if (
                target is None
                or target.chat_id != chat_id
                or target.role != "user"
                or target.chat_seq != delete_from_chat_seq
            ):
                raise ValueError("replacement target is no longer a valid user message")
            self.db.query(ChatMessage).filter(
                ChatMessage.chat_id == chat_id,
                ChatMessage.chat_seq >= delete_from_chat_seq,
            ).delete(synchronize_session=False)
            user_message = ChatMessage(
                message_id=user_message_id,
                chat_id=chat_id,
                chat_seq=first_seq,
                role="user",
                content=clamp_message_content(user_content),
                model=model,
                extra_data=dict(user_extra_data or {}),
                created_at=now,
            )
            run = ChatRun(
                run_id=run_id,
                chat_id=chat_id,
                user_id=user_id,
                message_id=assistant_message_id,
                user_message_id=user_message_id,
                user_chat_seq=first_seq,
                assistant_chat_seq=first_seq + 1,
                writer_slot=MAIN_WRITER_SLOT,
                status="pending",
                request_payload=dict(request_payload or {}),
                last_event_offset=0,
                created_at=now,
            )
            assistant_message = inflight.new_assistant_row(
                message_id=assistant_message_id,
                chat_id=chat_id,
                chat_seq=first_seq + 1,
                run_id=run_id,
                model=model,
                created_at=now,
            )
            self.db.add_all((user_message, assistant_message, run))
            self._recount(chat_id, now, touch_last_message=True)
            self.db.commit()
            self.db.refresh(user_message)
            self.db.refresh(run)
            return AcceptedChatRun(run=run, user_message=user_message)
        except IntegrityError as exc:
            self.db.rollback()
            winner = self.get_active_run(chat_id)
            if winner is not None:
                raise ChatBusyError(winner) from exc
            raise
        except Exception:
            self.db.rollback()
            raise

    def mark_running(self, run_id: str) -> bool:
        """CAS a durably accepted run from pending to running."""

        affected = (
            self.db.query(ChatRun)
            .filter(ChatRun.run_id == run_id, ChatRun.status == "pending")
            .update(
                {"status": "running", "started_at": datetime.now(timezone.utc)},
                synchronize_session=False,
            )
        )
        self.db.commit()
        return bool(affected)

    def release_writer(self, run_id: str, *, terminal_status: str) -> bool:
        """CAS a live run to terminal and release its unique writer slot."""

        if terminal_status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"not a terminal status: {terminal_status}")
        affected = (
            self.db.query(ChatRun)
            .filter(ChatRun.run_id == run_id, ChatRun.status.in_(LIVE_RUN_STATUSES))
            .update(
                {
                    "status": terminal_status,
                    "writer_slot": None,
                    "completed_at": datetime.now(timezone.utc),
                },
                synchronize_session=False,
            )
        )
        self.db.commit()
        return bool(affected)

    def abandon_pending_run(self, run_id: str, *, reason: str) -> bool:
        """Release an accepted run when preparation/launch fails synchronously."""

        self.db.rollback()
        affected = (
            self.db.query(ChatRun)
            .filter(ChatRun.run_id == run_id, ChatRun.status == "pending")
            .update(
                {
                    "status": "failed",
                    "writer_slot": None,
                    "error_message": (reason or "run launch failed")[:1000],
                    "completed_at": datetime.now(timezone.utc),
                },
                synchronize_session=False,
            )
        )
        self.db.commit()
        return bool(affected)

    def fail_run(self, run_id: str, *, reason: str) -> bool:
        """Fail a pending/running run and release its writer slot."""

        self.db.rollback()
        affected = (
            self.db.query(ChatRun)
            .filter(ChatRun.run_id == run_id, ChatRun.status.in_(LIVE_RUN_STATUSES))
            .update(
                {
                    "status": "failed",
                    "writer_slot": None,
                    "error_message": (reason or "run failed")[:1000],
                    "completed_at": datetime.now(timezone.utc),
                },
                synchronize_session=False,
            )
        )
        self.db.commit()
        return bool(affected)
