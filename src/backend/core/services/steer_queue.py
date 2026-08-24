"""Database-authoritative queue for steer, follow-up and next-run inputs."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from core.db.engine import SessionLocal
from core.db.models import ChatRun, ChatSteerQueueItem

DELIVERY_MODES = ("steer", "follow_up", "next_run")
QUEUE_STATUSES = ("accepted", "claimed", "applied", "cancelled", "superseded")
LIVE_RUN_STATUSES = ("pending", "running")


class SteerQueueError(RuntimeError):
    pass


class SteerQueueConflict(SteerQueueError):
    pass


class SteerClaimLost(SteerQueueError):
    pass


@dataclass(frozen=True)
class SteerRecord:
    queue_id: str
    steer_id: str
    chat_id: str
    user_id: str
    target_run_id: Optional[str]
    steer_seq: int
    target_operation_seq: Optional[int]
    delivery_mode: str
    status: str
    message: str
    claim_owner: Optional[str]
    lease_expires_at: Optional[datetime]
    delivery_attempt: int
    superseded_by: Optional[str]
    applied_run_id: Optional[str]
    applied_source_run_id: Optional[str]
    applied_operation_seq: Optional[int]

    def as_payload(self) -> dict:
        return {
            "queue_id": self.queue_id,
            "steer_id": self.steer_id,
            "run_id": self.target_run_id,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "steer_seq": self.steer_seq,
            "target_operation_seq": self.target_operation_seq,
            "delivery_mode": self.delivery_mode,
            "status": self.status,
            "message": self.message,
            "claim_owner": self.claim_owner,
            "lease_expires_at": (
                self.lease_expires_at.isoformat() if self.lease_expires_at else None
            ),
            "delivery_attempt": self.delivery_attempt,
            "superseded_by": self.superseded_by,
            "applied_run_id": self.applied_run_id,
            "applied_source_run_id": self.applied_source_run_id,
            "applied_operation_seq": self.applied_operation_seq,
        }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _record(row: ChatSteerQueueItem) -> SteerRecord:
    return SteerRecord(
        queue_id=str(row.queue_id),
        steer_id=str(row.steer_id),
        chat_id=str(row.chat_id),
        user_id=str(row.user_id),
        target_run_id=(str(row.target_run_id) if row.target_run_id else None),
        steer_seq=int(row.steer_seq),
        target_operation_seq=(
            int(row.target_operation_seq) if row.target_operation_seq is not None else None
        ),
        delivery_mode=str(row.delivery_mode),
        status=str(row.status),
        message=str(row.message),
        claim_owner=(str(row.lease_owner) if row.lease_owner else None),
        lease_expires_at=row.lease_expires_at,
        delivery_attempt=int(row.delivery_attempt or 0),
        superseded_by=(str(row.superseded_by) if row.superseded_by else None),
        applied_run_id=(str(row.applied_run_id) if row.applied_run_id else None),
        applied_source_run_id=(
            str(row.applied_source_run_id) if row.applied_source_run_id else None
        ),
        applied_operation_seq=(
            int(row.applied_operation_seq)
            if row.applied_operation_seq is not None
            else None
        ),
    )


def normalize_delivery_mode(value: str) -> str:
    aliases = {
        "steer": "steer",
        "followup": "follow_up",
        "follow_up": "follow_up",
        "nextrun": "next_run",
        "next_run": "next_run",
    }
    normalized = aliases.get(str(value or "steer").replace("-", "_").lower())
    if normalized is None:
        raise ValueError(f"unsupported delivery mode: {value}")
    return normalized


def _idempotent_record_or_conflict(
    row: ChatSteerQueueItem,
    *,
    target_run_id: Optional[str],
    user_id: str,
    delivery_mode: str,
    message: str,
) -> SteerRecord:
    same_request = (
        (str(row.target_run_id) if row.target_run_id else None) == target_run_id
        and str(row.user_id) == user_id
        and str(row.delivery_mode) == delivery_mode
        and str(row.message) == message
    )
    if not same_request:
        raise SteerQueueConflict(
            "steer_id already belongs to a different run, message, or delivery mode"
        )
    return _record(row)


class SteerQueue:
    """Short-transaction state machine; Redis is only a wake-up hint."""

    def __init__(
        self,
        session_factory: sessionmaker = SessionLocal,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock

    def accept(
        self,
        *,
        target_run_id: Optional[str],
        chat_id: str,
        user_id: str,
        steer_id: str,
        message: str,
        delivery_mode: str = "steer",
        replace_latest: bool = True,
    ) -> SteerRecord:
        mode = normalize_delivery_mode(delivery_mode)
        text = str(message or "").strip()
        if not text or len(text) > 10_000:
            raise ValueError("steer message must contain 1..10000 characters")
        if not steer_id or len(str(steer_id)) > 64:
            raise ValueError("steer_id must contain 1..64 characters")
        if not target_run_id:
            raise SteerQueueConflict(f"{mode} requires an active target run")

        for attempt in range(8):
            now = self._clock()
            queue_id = f"steerq_{uuid.uuid4().hex}"
            try:
                with self._sessions() as db:
                    existing = (
                        db.query(ChatSteerQueueItem)
                        .filter(
                            ChatSteerQueueItem.chat_id == chat_id,
                            ChatSteerQueueItem.steer_id == str(steer_id),
                        )
                        .one_or_none()
                    )
                    if existing is not None:
                        return _idempotent_record_or_conflict(
                            existing,
                            target_run_id=target_run_id,
                            user_id=user_id,
                            delivery_mode=mode,
                            message=text,
                        )

                    target_operation_seq: Optional[int] = None
                    if target_run_id:
                        run = (
                            db.query(ChatRun)
                            .filter(ChatRun.run_id == target_run_id)
                            .with_for_update()
                            .one_or_none()
                        )
                        if run is None or run.chat_id != chat_id or run.user_id != user_id:
                            raise SteerQueueConflict("target run does not belong to this chat/user")
                        if run.status not in LIVE_RUN_STATUSES:
                            raise SteerQueueConflict("target run is no longer active")
                        target_operation_seq = int(run.operation_seq or 0) + 1

                    next_seq = int(
                        db.query(func.max(ChatSteerQueueItem.steer_seq))
                        .filter(ChatSteerQueueItem.chat_id == chat_id)
                        .scalar()
                        or 0
                    ) + 1
                    if replace_latest and mode == "steer":
                        db.query(ChatSteerQueueItem).filter(
                            ChatSteerQueueItem.target_run_id == target_run_id,
                            ChatSteerQueueItem.delivery_mode == "steer",
                            ChatSteerQueueItem.status == "accepted",
                        ).update(
                            {
                                ChatSteerQueueItem.status: "superseded",
                                ChatSteerQueueItem.superseded_by: queue_id,
                                ChatSteerQueueItem.updated_at: now,
                            },
                            synchronize_session=False,
                        )
                    row = ChatSteerQueueItem(
                        queue_id=queue_id,
                        steer_id=str(steer_id),
                        chat_id=chat_id,
                        user_id=user_id,
                        target_run_id=target_run_id,
                        steer_seq=next_seq,
                        target_operation_seq=target_operation_seq,
                        delivery_mode=mode,
                        status="accepted",
                        message=text,
                        delivery_attempt=0,
                        accepted_at=now,
                        updated_at=now,
                    )
                    db.add(row)
                    db.commit()
                    db.refresh(row)
                    return _record(row)
            except IntegrityError:
                # A concurrent writer may have won either idempotency or seq.
                with self._sessions() as db:
                    existing = (
                        db.query(ChatSteerQueueItem)
                        .filter(
                            ChatSteerQueueItem.chat_id == chat_id,
                            ChatSteerQueueItem.steer_id == str(steer_id),
                        )
                        .one_or_none()
                    )
                    if existing is not None:
                        return _idempotent_record_or_conflict(
                            existing,
                            target_run_id=target_run_id,
                            user_id=user_id,
                            delivery_mode=mode,
                            message=text,
                        )
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 7:
                    raise
            time.sleep(0.005 * (attempt + 1))
        raise SteerQueueConflict("could not allocate a monotonic steer sequence")

    def claim_next(
        self,
        target_run_id: str,
        *,
        owner: str,
        lease_seconds: int = 90,
    ) -> Optional[SteerRecord]:
        if not owner:
            raise ValueError("claim owner is required")
        for _attempt in range(8):
            now = self._clock()
            expires = now + timedelta(seconds=max(1, int(lease_seconds)))
            with self._sessions() as db:
                candidate = (
                    db.query(ChatSteerQueueItem)
                    .filter(
                        ChatSteerQueueItem.target_run_id == target_run_id,
                        ChatSteerQueueItem.delivery_mode == "steer",
                        or_(
                            ChatSteerQueueItem.status == "accepted",
                            and_(
                                ChatSteerQueueItem.status == "claimed",
                                ChatSteerQueueItem.lease_expires_at <= now,
                            ),
                        ),
                    )
                    .order_by(ChatSteerQueueItem.steer_seq)
                    .first()
                )
                if candidate is None:
                    return None
                affected = (
                    db.query(ChatSteerQueueItem)
                    .filter(
                        ChatSteerQueueItem.queue_id == candidate.queue_id,
                        or_(
                            ChatSteerQueueItem.status == "accepted",
                            and_(
                                ChatSteerQueueItem.status == "claimed",
                                ChatSteerQueueItem.lease_expires_at <= now,
                            ),
                        ),
                    )
                    .update(
                        {
                            ChatSteerQueueItem.status: "claimed",
                            ChatSteerQueueItem.lease_owner: owner,
                            ChatSteerQueueItem.lease_expires_at: expires,
                            ChatSteerQueueItem.claimed_at: now,
                            ChatSteerQueueItem.delivery_attempt: (
                                ChatSteerQueueItem.delivery_attempt + 1
                            ),
                            ChatSteerQueueItem.updated_at: now,
                        },
                        synchronize_session=False,
                    )
                )
                db.commit()
                if affected:
                    db.expire_all()
                    claimed = db.get(ChatSteerQueueItem, candidate.queue_id)
                    return _record(claimed)
        return None

    @staticmethod
    def mark_applied_in_session(
        db: Session,
        *,
        queue_id: str,
        claim_owner: str,
        applied_run_id: str,
        applied_operation_seq: int,
        now: Optional[datetime] = None,
    ) -> bool:
        applied_at = now or _utcnow()
        affected = (
            db.query(ChatSteerQueueItem)
            .filter(
                ChatSteerQueueItem.queue_id == queue_id,
                ChatSteerQueueItem.status == "claimed",
                ChatSteerQueueItem.lease_owner == claim_owner,
            )
            .update(
                {
                    ChatSteerQueueItem.status: "applied",
                    ChatSteerQueueItem.applied_run_id: applied_run_id,
                    ChatSteerQueueItem.applied_source_run_id: applied_run_id,
                    ChatSteerQueueItem.applied_operation_seq: int(applied_operation_seq),
                    ChatSteerQueueItem.applied_at: applied_at,
                    ChatSteerQueueItem.lease_owner: None,
                    ChatSteerQueueItem.lease_expires_at: None,
                    ChatSteerQueueItem.updated_at: applied_at,
                },
                synchronize_session=False,
            )
        )
        if not affected:
            raise SteerClaimLost(queue_id)
        return True

    @staticmethod
    def consume_next_handoff_in_session(
        db: Session,
        *,
        source_run_id: str,
        root_run_id: Optional[str] = None,
        chat_id: str,
        applied_run_id: Optional[str] = None,
        applied_operation_seq: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> Optional[SteerRecord]:
        applied_at = now or _utcnow()
        follow_up_targets = {source_run_id, str(root_run_id or source_run_id)}
        candidate = (
            db.query(ChatSteerQueueItem)
            .filter(
                ChatSteerQueueItem.chat_id == chat_id,
                ChatSteerQueueItem.status == "accepted",
                or_(
                    and_(
                        ChatSteerQueueItem.delivery_mode == "follow_up",
                        ChatSteerQueueItem.target_run_id.in_(follow_up_targets),
                    ),
                    ChatSteerQueueItem.delivery_mode == "next_run",
                ),
            )
            .order_by(ChatSteerQueueItem.steer_seq)
            .with_for_update()
            .first()
        )
        if candidate is None:
            return None
        # The queue identity makes crash recovery and a repeated completion
        # attempt converge on the same next Run instead of double-writing.
        next_run_id = applied_run_id or (
            f"run_{uuid.uuid5(uuid.NAMESPACE_URL, f'steer-handoff:{candidate.queue_id}').hex[:16]}"
        )
        affected = (
            db.query(ChatSteerQueueItem)
            .filter(
                ChatSteerQueueItem.queue_id == candidate.queue_id,
                ChatSteerQueueItem.status == "accepted",
            )
            .update(
                {
                    ChatSteerQueueItem.status: "applied",
                    ChatSteerQueueItem.applied_run_id: next_run_id,
                    ChatSteerQueueItem.applied_source_run_id: source_run_id,
                    ChatSteerQueueItem.applied_operation_seq: applied_operation_seq,
                    ChatSteerQueueItem.applied_at: applied_at,
                    ChatSteerQueueItem.delivery_attempt: (
                        ChatSteerQueueItem.delivery_attempt + 1
                    ),
                    ChatSteerQueueItem.updated_at: applied_at,
                },
                synchronize_session=False,
            )
        )
        if not affected:
            return None
        db.flush()
        db.expire_all()
        return _record(db.get(ChatSteerQueueItem, candidate.queue_id))

    def cancel(self, target_run_id: str, steer_id: str) -> bool:
        now = self._clock()
        with self._sessions() as db:
            affected = (
                db.query(ChatSteerQueueItem)
                .filter(
                    ChatSteerQueueItem.target_run_id == target_run_id,
                    ChatSteerQueueItem.steer_id == steer_id,
                    or_(
                        ChatSteerQueueItem.status == "accepted",
                        and_(
                            ChatSteerQueueItem.status == "claimed",
                            ChatSteerQueueItem.lease_expires_at <= now,
                        ),
                    ),
                )
                .update(
                    {
                        ChatSteerQueueItem.status: "cancelled",
                        ChatSteerQueueItem.cancelled_at: now,
                        ChatSteerQueueItem.lease_owner: None,
                        ChatSteerQueueItem.lease_expires_at: None,
                        ChatSteerQueueItem.updated_at: now,
                    },
                    synchronize_session=False,
                )
            )
            db.commit()
            return bool(affected)

    def list_for_run(self, target_run_id: str) -> list[SteerRecord]:
        with self._sessions() as db:
            rows = (
                db.query(ChatSteerQueueItem)
                .filter(
                    or_(
                        ChatSteerQueueItem.target_run_id == target_run_id,
                        ChatSteerQueueItem.applied_source_run_id == target_run_id,
                    )
                )
                .order_by(ChatSteerQueueItem.steer_seq)
                .all()
            )
            return [_record(row) for row in rows]


__all__ = [
    "DELIVERY_MODES",
    "QUEUE_STATUSES",
    "SteerClaimLost",
    "SteerQueue",
    "SteerQueueConflict",
    "SteerQueueError",
    "SteerRecord",
    "normalize_delivery_mode",
]
