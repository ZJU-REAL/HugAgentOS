"""Reconstruct Episodes from historical logs (GCE ticket 08).

Because the join key is the assistant ``message_id`` — already present on every
pre-existing log table — history does not have to be waited for.  A single
backfill pass yields tens of thousands of Episodes on day one, which is what
lets attribution rules be tuned and pattern mining be validated weeks earlier
than "start collecting and check back later".

**The boundary that makes this safe:** backfilled Episodes carry no asset
snapshot, because the Runtime Binder did not exist when they ran.  They are
marked ``backfilled`` and are refused by counterfactual replay — replay's entire
premise is "everything else held fixed", and for these runs we simply do not
know what everything else was.  They remain fully usable for pattern discovery
and rule calibration, which is what they are for.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from core.db.engine import SessionLocal
from core.evolution.trace_assembler import assemble_episode

logger = logging.getLogger(__name__)

# Bounded so a backfill of a large history cannot monopolise the database.
DEFAULT_BATCH_SIZE = 200


def _candidate_messages(
    db, *, since: datetime, until: datetime, limit: int, offset: int
) -> List[tuple]:
    """Assistant messages in the window that have no Episode yet."""
    from core.db.models import ChatMessage
    from core.db.models.evolution import EvolutionEpisode

    existing = db.query(EvolutionEpisode.message_id).subquery()
    rows = (
        db.query(
            ChatMessage.message_id,
            ChatMessage.chat_id,
            ChatMessage.created_at,
            ChatMessage.chat_seq,
        )
        .filter(
            ChatMessage.role == "assistant",
            ChatMessage.created_at >= since,
            ChatMessage.created_at < until,
            ~ChatMessage.message_id.in_(db.query(existing.c.message_id)),
        )
        .order_by(ChatMessage.created_at.asc(), ChatMessage.chat_seq.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return rows


def _objective_for(db, chat_id: str, before_seq: int) -> str:
    """The user turn that prompted this answer, used as the objective."""
    from core.db.models import ChatMessage

    row = (
        db.query(ChatMessage.content)
        .filter(
            ChatMessage.chat_id == chat_id,
            ChatMessage.role == "user",
            ChatMessage.chat_seq <= before_seq,
        )
        .order_by(ChatMessage.chat_seq.desc())
        .first()
    )
    return (row[0] if row else "") or ""


def _user_for(db, chat_id: str) -> str:
    from core.db.models import ChatSession

    row = db.query(ChatSession.user_id).filter(ChatSession.chat_id == chat_id).first()
    return (row[0] if row else "") or ""


def _outcome_from_logs(db, message_id: str, chat_id: str) -> Optional[Dict[str, object]]:
    """Derive a verdict for a historical run from what was actually recorded.

    Why this exists: without a verdict every backfilled Episode reads as
    ``unknown``, and both mining paths require ``success`` — pattern success
    rates need it and ordering-constraint extraction skips anything else. So a
    backfill that records no outcome produces evidence that can never support a
    candidate. Measured on this deployment: 15 backfilled Episodes, 15
    ``unknown``, 0 patterns, 0 candidates.

    The signal is deliberately narrow and labelled weak:

    * any tool call that did not succeed → ``failed``;
    * at least one tool call, all successful, and the answer itself carries no
      error → ``success``, at a quality score that clears the mining threshold
      without pretending to be a graded judgement;
    * no tool calls → no verdict at all. There is nothing here to learn an
      ordering from, and inventing "success" would inflate every rate the
      console reports.

    It is a proxy, not ground truth — an answer whose tools all returned 200 can
    still be wrong. ``verdict_source`` records that, so a reviewer can tell this
    apart from an explicit user judgement and weight it accordingly.
    """
    from core.db.models import ChatMessage
    from core.evolution.trace_assembler import _tool_rows_for

    rows = _tool_rows_for(db, message_id, chat_id)
    if not rows:
        return None

    failed = [r for r in rows if str(r.status or "success") != "success"]
    message = (
        db.query(ChatMessage).filter(ChatMessage.message_id == message_id).first()
    )
    answered = bool(message is not None and (message.content or "").strip())
    message_errored = bool(message is not None and message.error)

    if failed or message_errored or not answered:
        return {
            "verdict": "failed",
            "verdict_source": "backfill_tool_status",
            "signals": ["tool_status"],
            "weak": True,
            "failed_tool_calls": len(failed),
        }
    return {
        "verdict": "success",
        "verdict_source": "backfill_tool_status",
        "signals": ["tool_status"],
        "weak": True,
        "quality_score": 0.7,
        "tool_calls": len(rows),
    }


def backfill_episodes(
    *,
    days: int = 30,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: Optional[int] = None,
    until: Optional[datetime] = None,
) -> Dict[str, int]:
    """Reconstruct Episodes for assistant messages that do not have one.

    Idempotent by construction: messages that already have an Episode are
    filtered out by the query, and ``assemble_episode`` is itself a no-op on
    repeat. Safe to re-run after an interruption — it simply resumes.
    """
    end = until or datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    stats = {"scanned": 0, "created": 0, "skipped": 0, "batches": 0}
    offset = 0

    while True:
        if max_batches is not None and stats["batches"] >= max_batches:
            logger.info("[backfill] batch cap reached, stopping early")
            break

        try:
            with SessionLocal() as db:
                rows = _candidate_messages(
                    db, since=start, until=end, limit=batch_size, offset=0
                )
                if not rows:
                    break

                for message_id, chat_id, created_at, chat_seq in rows:
                    stats["scanned"] += 1
                    objective = _objective_for(db, chat_id, chat_seq)
                    user_id = _user_for(db, chat_id)

                    # Historical runs have tool logs but no trace events;
                    # bridge them so the backfill is actually usable for mining.
                    from core.evolution.events import TraceSink
                    from core.evolution.trace_assembler import emit_tool_events

                    sink = TraceSink(
                        message_id=message_id, chat_id=chat_id or "", user_id=user_id
                    )
                    if emit_tool_events(sink, message_id, chat_id or ""):
                        sink.flush()

                    episode_id = assemble_episode(
                        message_id=message_id,
                        run_id="",
                        chat_id=chat_id or "",
                        user_id=user_id,
                        objective=objective,
                        task_type="chat",
                        # A weak, log-derived verdict. Without one the episode is
                        # unusable for mining; see _outcome_from_logs.
                        outcome=_outcome_from_logs(db, message_id, chat_id or ""),
                        # No bundle exists for historical runs; this is exactly
                        # what the backfilled flag records.
                        bundle=None,
                        completed_at=created_at,
                        backfilled=True,
                    )
                    if episode_id:
                        stats["created"] += 1
                    else:
                        stats["skipped"] += 1
        except Exception as exc:
            logger.warning("[backfill] batch failed, stopping: %s", exc)
            break

        stats["batches"] += 1
        # The filter excludes already-created episodes, so the next query
        # naturally advances; no offset arithmetic needed and no risk of
        # skipping rows when a batch partially fails.
        if stats["created"] == 0 and stats["skipped"] >= len(rows):
            break

    logger.info(
        "[backfill] done: scanned=%(scanned)d created=%(created)d skipped=%(skipped)d",
        stats,
    )
    return stats
