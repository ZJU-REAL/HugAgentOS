"""User evaluation of finished tasks (assistant answers): verdicts, reasons and reminders.

A task's answer gets one of three verdicts — ``adopt`` (满意), ``revise`` (需修改) or ``reject``
(不满意). ``revise`` / ``reject`` may carry reasons picked from preset dimensions and a comment.

Storage is additive on ``message_feedback``: the verdict lives in ``verdict`` and the legacy
``rating`` column keeps its like/dislike values (``adopt`` → like, otherwise dislike) so older
consumers and older releases rolled back onto the same database keep working. Rows written before
verdicts existed have ``verdict`` NULL and are read through :func:`effective_verdict`.

Unrated answers follow the "simple tasks are not chased, complex tasks are not missed" rule:
an answer is reminded about only inside :data:`REMINDER_WORKING_DAYS` working days after it was
delivered. Past that, a simple task counts as adopted by default and a complex one goes to the
post-hoc spot check pool. Nothing is written for either; both are derived when read.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import and_, func, not_, select
from sqlalchemy.orm import Session

from core.chat.sidebar_status import BACKGROUND_KINDS
from core.db.models import ChatMessage, ChatRun, ChatSession, MessageFeedback
from core.db.reserved_chats import ordinary_chat_filter
from core.infra.time import as_utc, utc_now

ADOPT, REVISE, REJECT = "adopt", "revise", "reject"
VERDICTS: tuple[str, ...] = (ADOPT, REVISE, REJECT)
VERDICT_LABELS: Dict[str, str] = {ADOPT: "满意", REVISE: "需修改", REJECT: "不满意"}

# Preset reasons offered for revise / reject, in display order. ``accuracy`` and ``efficiency``
# keep their keys from the first version (same meaning), so earlier records still read right.
REASONS: Dict[str, str] = {
    "accuracy": "模型回答不准确",
    "source": "数据获取来源不准确",
    "calculation": "数据统计计算错误",
    "definition": "数据口径出错",
    "intent": "模型误解用户意图",
    "efficiency": "模型回答效率低",
}
# Retired reasons: no longer offered, but records that carry them still display.
LEGACY_REASONS: Dict[str, str] = {
    "completeness": "完整性",
    "actionability": "可操作性",
}
REASON_LABELS: Dict[str, str] = {**REASONS, **LEGACY_REASONS}

# Derived states for answers nobody rated.
DEFAULT_ADOPT = "default_adopt"  # simple task, reminder window passed
SPOT_CHECK = "spot_check"        # complex task, reminder window passed
PENDING = "pending"              # still inside the reminder window
NOT_NEEDED = "not_needed"        # the evaluation gate judged it not worth rating (switch on only)

REMINDER_WORKING_DAYS = 3
REMINDER_LIMIT = 5
COMMENT_MAX = 2000



# Scheduled and loop tasks run as chat turns, but nobody asked in person and nobody waits on the
# answer: they have no evaluation (no card, no reminder, no satisfaction statistics).
NOT_EVALUATED_KINDS = (*BACKGROUND_KINDS, "autonomous_loop")


def is_evaluable(payload: Optional[Dict[str, Any]]) -> bool:
    """Whether a run (by its request payload) is a task a user asked for in person."""
    payload = payload if isinstance(payload, dict) else {}
    return (
        str(payload.get("kind") or "chat") not in NOT_EVALUATED_KINDS
        and not payload.get("automation_task_id") and not payload.get("loop_id")
    )


def evaluable_run_filter():
    """SQL twin of :func:`is_evaluable` on ``ChatRun``."""
    kind = func.coalesce(ChatRun.request_payload["kind"].as_string(), "chat")
    return and_(
        kind.not_in(NOT_EVALUATED_KINDS),
        ChatRun.request_payload["automation_task_id"].as_string().is_(None),
        ChatRun.request_payload["loop_id"].as_string().is_(None),
    )


def excluded_messages(db: Session, chat_id: str) -> List[str]:
    """Answers in one chat that belong to scheduled or loop tasks (shown without evaluation)."""
    rows = db.query(ChatRun.message_id).filter(
        ChatRun.chat_id == chat_id, ChatRun.message_id.is_not(None), not_(evaluable_run_filter())
    )
    return sorted({mid for (mid,) in rows})


def message_evaluable(db: Session, message_id: str) -> bool:
    """False when the answer came from a scheduled or loop task."""
    return db.query(ChatRun.run_id).filter(
        ChatRun.message_id == message_id, not_(evaluable_run_filter())
    ).first() is None


def legacy_rating(verdict: str) -> str:
    """The like/dislike value the legacy ``rating`` column stores for ``verdict``."""
    return "like" if verdict == ADOPT else "dislike"


def normalize_verdict(value: Optional[str]) -> Optional[str]:
    """Accept a verdict or a legacy like/dislike rating; ``None`` for anything else."""
    if value in VERDICTS:
        return value
    if value == "like":
        return ADOPT
    if value == "dislike":
        return REJECT
    return None


def effective_verdict(rating: Optional[str], verdict: Optional[str]) -> Optional[str]:
    """Verdict of a stored feedback row, falling back to its legacy rating."""
    return normalize_verdict(verdict) or normalize_verdict(rating)


def clean_reasons(values: Optional[Iterable[str]]) -> List[str]:
    """Reasons a user may pick now, in display order; duplicates and unknown keys dropped."""
    picked = set(values or ())
    return [key for key in REASONS if key in picked]


def stored_reasons(values: Optional[Iterable[str]]) -> List[str]:
    """Reasons on a saved record, retired ones included, in display order."""
    picked = set(values or ())
    return [key for key in REASON_LABELS if key in picked]


def _local_date(value: datetime) -> date:
    return as_utc(value).astimezone().date()


def working_days_between(start: datetime, end: datetime) -> int:
    """Whole working days (Mon–Fri) after ``start``'s day up to and including ``end``'s day.

    Public holidays are not modelled; weekends are the only days that do not count.
    """
    first, last = _local_date(start), _local_date(end)
    if last <= first:
        return 0
    days = 0
    cursor = first + timedelta(days=1)
    while cursor <= last:
        if cursor.weekday() < 5:
            days += 1
        cursor += timedelta(days=1)
    return days


def reminder_cutoff(now: Optional[datetime] = None) -> datetime:
    """Earliest delivery time still inside the reminder window (UTC).

    An answer delivered on day D stays pending through the ``REMINDER_WORKING_DAYS``-th working
    day after D; the cutoff is the start of the earliest local day that still satisfies that.
    """
    now = now or utc_now()
    today = _local_date(now)
    day = today
    counted = 0
    while counted < REMINDER_WORKING_DAYS:
        if day.weekday() < 5:
            counted += 1
        day -= timedelta(days=1)
    # ``day`` is now the earliest delivery day whose window still includes today.
    local_start = datetime.combine(day, datetime.min.time()).astimezone()
    return local_start.astimezone(timezone.utc)


def unrated_state(delivered_at: Optional[datetime], *, complex_task: bool,
                  now: Optional[datetime] = None, need_evaluation: bool = True) -> str:
    """Derived state of an answer nobody rated: pending, default_adopt or spot_check.

    ``need_evaluation=False`` (the evaluation gate said so, switch on) is ``not_needed``: never
    reminded, never adopted by default, never sent to the spot check pool.
    """
    if not need_evaluation:
        return NOT_NEEDED
    if delivered_at is None:
        return PENDING
    if working_days_between(delivered_at, now or utc_now()) <= REMINDER_WORKING_DAYS:
        return PENDING
    return SPOT_CHECK if complex_task else DEFAULT_ADOPT


def feedback_to_dict(row: MessageFeedback) -> Dict[str, Any]:
    return {
        "feedback_id": row.feedback_id,
        "message_id": row.message_id,
        "verdict": effective_verdict(row.rating, row.verdict),
        "reasons": stored_reasons(row.reasons or []),
        "comment": row.comment or "",
        "updated_at": as_utc(row.updated_at).isoformat() if row.updated_at else None,
    }


def chat_feedback(db: Session, chat_id: str, user_id: str) -> Dict[str, Dict[str, Any]]:
    """This user's feedback on every message of one chat, keyed by message id."""
    rows = db.query(MessageFeedback).filter(
        MessageFeedback.chat_id == chat_id, MessageFeedback.user_id == user_id
    )
    return {row.message_id: feedback_to_dict(row) for row in rows}


def pending_evaluations(db: Session, user_id: str, *, limit: int = REMINDER_LIMIT,
                        now: Optional[datetime] = None) -> Dict[str, Any]:
    """Unrated answers of the user's own chats that are still inside the reminder window.

    ``items`` holds the ``limit`` most recent conversations with something to rate (each pointing
    at its latest unrated answer); ``chat_ids`` lists every such conversation so the history list
    can mark them; ``total`` counts unrated answers; ``gate_enabled`` is the evaluation gate
    switch these lists were filtered with (read once, so the flag and the lists agree).
    """
    from core.chat import evaluation_gate
    from core.config.store import evaluation_gate_options as gate_options

    gate = gate_options.evaluation_gate_options()
    gating_start = evaluation_gate.gating_window_start(gate.timeout_ms, now)
    cutoff = reminder_cutoff(now)
    delivered = func.coalesce(ChatRun.completed_at, ChatRun.updated_at)
    columns = [ChatRun.chat_id, ChatRun.message_id, delivered.label("delivered_at"),
               ChatSession.title]
    if gate.enabled:  # the gate's JSON is only worth extracting when it is applied
        columns += [ChatMessage.extra_data[evaluation_gate.STORAGE_KEY].label("gate"),
                    ChatMessage.extra_data[evaluation_gate.PENDING_KEY].label("gate_pending")]
    rows = db.execute(
        select(*columns)
        .join(ChatSession, ChatSession.chat_id == ChatRun.chat_id)
        # The answer must still exist (edit-and-resend removes the old one).
        .join(ChatMessage, ChatMessage.message_id == ChatRun.message_id)
        .outerjoin(MessageFeedback, and_(MessageFeedback.message_id == ChatRun.message_id,
                                         MessageFeedback.user_id == user_id))
        .where(
            ChatRun.user_id == user_id, ChatSession.user_id == user_id,
            ChatSession.deleted_at.is_(None), ChatSession.channel_id.is_(None),
            ordinary_chat_filter(ChatRun.chat_id), evaluable_run_filter(),
            ChatRun.status == "completed", ChatRun.message_id.is_not(None),
            # updated_at is never earlier than completed_at: an indexable superset of the window.
            ChatRun.updated_at >= cutoff,
            delivered >= cutoff, MessageFeedback.feedback_id.is_(None),
        )
        .order_by(delivered.desc(), ChatRun.run_id.desc())
    ).all()
    # A rerun replaces the earlier answer; only the newest answer per message counts once.
    seen_messages: set[str] = set()
    per_chat: Dict[str, Dict[str, Any]] = {}
    total = 0
    for row in rows:
        if row.message_id in seen_messages:
            continue
        seen_messages.add(row.message_id)
        # With the evaluation gate on, answers judged not worth rating are not chased, nor are
        # answers whose decision is still being made (they may turn out not to need one).
        if gate.enabled:
            state = evaluation_gate.gate_state(row.gate, row.gate_pending)
            if state.not_needed or state.gating("completed", row.delivered_at, gating_start):
                continue
        total += 1
        entry = per_chat.get(row.chat_id)
        if entry is None:
            per_chat[row.chat_id] = {
                "chat_id": row.chat_id,
                "title": row.title or "",
                "message_id": row.message_id,
                "delivered_at": as_utc(row.delivered_at).isoformat() if row.delivered_at else None,
                "unrated": 1,
            }
        else:
            entry["unrated"] += 1
    ordered = list(per_chat.values())
    return {
        "items": ordered[:limit],
        "chat_ids": [item["chat_id"] for item in ordered],
        "total": total,
        "gate_enabled": gate.enabled,
    }
