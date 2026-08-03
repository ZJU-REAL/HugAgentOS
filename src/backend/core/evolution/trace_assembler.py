"""Assemble scattered logs into one Episode (GCE ticket 04).

A run's traces already exist across four tables — tool calls, skill calls,
sub-agent calls, ontology gate events — but nothing joins them, so "what
happened in this task?" has no answer.  This module joins them on the assistant
``message_id`` (pre-allocated by the run executor and already present on every
one of those tables) and writes a single Episode row.

Two properties this must hold:

* **Idempotent.**  A run yields exactly one Episode no matter how many times
  assembly is retried; the unique constraint on ``message_id`` is the backstop
  and the code checks first so retries are silent rather than noisy.
* **Off the critical path.**  Called after the SSE stream closes.  Nothing here
  may run while the user is waiting.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.db.engine import SessionLocal
from core.db.models.evolution import EvolutionEpisode, EvolutionTraceEvent
from core.evolution.contract import AssetBundle
from core.evolution.trace_store import attach_episode

logger = logging.getLogger(__name__)

_PREVIEW_MAX = 400


def _objective_hash(text: str) -> str:
    normalized = " ".join((text or "").split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def _preview(text: str) -> str:
    """Short, sanitized excerpt of the user's objective."""
    collapsed = " ".join((text or "").split())
    if not collapsed:
        return ""
    try:
        from core.memory.sanitizer import sanitize

        result = sanitize(collapsed)
        if result.reject:
            return "[REDACTED]"
        collapsed = result.text or collapsed
    except Exception:
        pass
    return collapsed[:_PREVIEW_MAX]


def _count_existing_logs(db, message_id: str) -> Dict[str, int]:
    """Fold the pre-existing log tables into the episode's shape.

    These stay in their own tables — we count and reference, never copy. A
    missing table (community edition, or a partially migrated database) yields
    zero rather than an exception.
    """
    counts = {"tool_calls": 0, "skill_calls": 0, "subagent_calls": 0, "ontology_events": 0}
    try:
        from core.db.models import SkillCallLog, SubAgentCallLog, ToolCallLog

        counts["tool_calls"] = (
            db.query(ToolCallLog).filter(ToolCallLog.message_id == message_id).count()
        )
        counts["skill_calls"] = (
            db.query(SkillCallLog).filter(SkillCallLog.message_id == message_id).count()
        )
        counts["subagent_calls"] = (
            db.query(SubAgentCallLog)
            .filter(SubAgentCallLog.message_id == message_id)
            .count()
        )
    except Exception as exc:
        logger.debug("[assembler] log counting degraded: %s", exc)
    return counts


def _tool_rows_for(db, message_id: str, chat_id: str = ""):
    """Tool calls belonging to one answer.

    Prefers the direct join on ``message_id``. Older rows were written before
    that id was stamped (the setter existed but had no caller), so a fallback is
    needed for history.

    The fallback deliberately does **not** compare timestamps across the two
    tables. Measured on real data they disagree by a fixed 8 hours — the two
    write paths land naive values into ``timestamptz`` under different
    assumptions — so any window built on that comparison silently matches
    nothing, or worse, matches the wrong turn. Rather than encode a correction
    for a bug that may be fixed later, the fallback covers only the case that
    needs no arithmetic: a chat with a single assistant answer, where every tool
    call in that chat unambiguously belongs to it.

    Multi-answer historical chats are left alone. New runs carry the id
    correctly, so this gap closes on its own as data accumulates.
    """
    from core.db.models import ChatMessage, ToolCallLog

    rows = (
        db.query(ToolCallLog)
        .filter(ToolCallLog.message_id == message_id)
        .order_by(ToolCallLog.created_at.asc())
        .all()
    )
    if rows or not chat_id:
        return rows

    assistant_count = (
        db.query(ChatMessage)
        .filter(ChatMessage.chat_id == chat_id, ChatMessage.role == "assistant")
        .count()
    )
    if assistant_count != 1:
        return []

    return (
        db.query(ToolCallLog)
        .filter(ToolCallLog.chat_id == chat_id)
        .order_by(ToolCallLog.created_at.asc())
        .all()
    )


# A tool call that reads a file under a skills directory is the model opening a
# skill for itself. Matching on the path is what makes the signal recoverable
# from history: the tool log has always carried the arguments, so every past run
# yields its skill opens too.
_SKILL_DOC_PATH = re.compile(r"(?:^|/)skills/([A-Za-z0-9_-]+)/(?:SKILL\.md|.+)$")
_FILE_READING_TOOLS = ("view_text_file", "read_file", "read")


def skill_id_from_path(file_path: str) -> str:
    """The skill a file path belongs to, or ``""``.

    Paths reach here in three shapes — the sandbox view
    (``/workspace/skills/<id>/SKILL.md``), the backend materialised view
    (``/app/storage/sandbox_skills/<id>/SKILL.md``) and the bundle view
    (``skill_bundles/default/<id>/SKILL.md``). All three end with the skill id
    followed by the file, so the id is recovered from the segment before the
    file rather than from any one root.
    """
    normalized = str(file_path or "").replace("\\", "/")
    match = _SKILL_DOC_PATH.search(normalized)
    if match:
        return match.group(1)
    # The materialised cache and bundle roots do not contain a literal
    # ``skills/`` segment, so fall back to "the directory holding SKILL.md".
    parts = [p for p in normalized.split("/") if p]
    if len(parts) >= 2 and parts[-1].upper() == "SKILL.MD":
        return parts[-2]
    return ""


def _opened_skill_id(row) -> str:
    """The skill this tool call opened, if it opened one."""
    if str(row.tool_name or "") not in _FILE_READING_TOOLS:
        return ""
    args = row.tool_args if isinstance(row.tool_args, dict) else {}
    for key in ("file_path", "path", "filepath", "file"):
        value = args.get(key)
        if value:
            skill_id = skill_id_from_path(str(value))
            if skill_id:
                return skill_id
    return ""


def emit_tool_events(sink, message_id: str, chat_id: str = "") -> int:
    """Bridge already-recorded tool calls into the trace stream.

    ``tool_call_logs`` has held the ordered calls all along; without this they
    never reached the evidence plane, so pattern mining saw episodes with no
    tool sequence and the promotion chain could never fire from real usage.

    Reading them here rather than emitting at call time keeps the response path
    free of extra writes and works for historical runs too.

    The same rows carry the *skill open* signal. A skill the model never opened
    was never used, however many times it was offered — and telling those two
    apart is what makes retirement a judgement rather than a coin flip.
    """
    if not message_id:
        return 0
    try:
        from core.evolution.events import EV_SKILL_OPENED, EV_TOOL_CALLED

        with SessionLocal() as db:
            rows = _tool_rows_for(db, message_id, chat_id)

        opened: Dict[str, int] = {}
        for position, row in enumerate(rows):
            if not row.tool_name:
                continue
            sink.append(
                EV_TOOL_CALLED,
                {
                    "tool_name": row.tool_name,
                    "status": row.status,
                    "duration_ms": row.duration_ms,
                    # Referenced, not copied: args and results stay in their own
                    # table so the event stream remains queryable at volume.
                    "tool_call_id": row.tool_call_id or "",
                },
                actor_type="tool",
                actor_id=row.tool_name,
            )
            skill_id = _opened_skill_id(row)
            if skill_id and skill_id not in opened:
                # Position is kept so follow-through can be measured: what the
                # model did *after* reading the skill is the only evidence that
                # the document actually shaped the plan.
                opened[skill_id] = position

        for skill_id, position in opened.items():
            following = [
                str(r.tool_name)
                for r in rows[position + 1 :]
                if r.tool_name and str(r.tool_name) not in _FILE_READING_TOOLS
            ]
            sink.append(
                EV_SKILL_OPENED,
                {
                    "skill_id": skill_id,
                    "position": position,
                    "tools_after_open": following[:30],
                },
                asset_kind="skill",
                actor_id=skill_id,
            )
        return len(rows)
    except Exception as exc:
        logger.debug("[assembler] tool event bridging skipped: %s", exc)
        return 0


def assemble_episode(
    *,
    message_id: str,
    run_id: str = "",
    chat_id: str = "",
    user_id: str = "",
    tenant_id: str = "default",
    objective: str = "",
    task_type: str = "chat",
    bundle: Optional[AssetBundle] = None,
    outcome: Optional[Dict[str, Any]] = None,
    started_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
    latency_ms: Optional[int] = None,
    privacy_class: str = "tenant",
    backfilled: bool = False,
) -> Optional[str]:
    """Build (or return the existing) Episode for a run.

    Returns the episode id, or ``None`` when assembly could not proceed.
    Never raises — the caller is a post-response background task and an
    assembly failure must not surface anywhere near the user.
    """
    if not message_id:
        return None

    try:
        with SessionLocal() as db:
            existing = (
                db.query(EvolutionEpisode)
                .filter(EvolutionEpisode.message_id == message_id)
                .first()
            )
            if existing is not None:
                # Retried assembly is a no-op, not an error.
                return existing.episode_id

            counts = _count_existing_logs(db, message_id)
            event_count = (
                db.query(EvolutionTraceEvent)
                .filter(EvolutionTraceEvent.message_id == message_id)
                .count()
            )

            episode_id = f"ep_{uuid.uuid4().hex[:20]}"
            bundle_dict = bundle.to_dict() if bundle is not None else None
            episode = EvolutionEpisode(
                episode_id=episode_id,
                run_id=run_id or None,
                message_id=message_id,
                chat_id=chat_id or None,
                user_id=user_id or None,
                tenant_id=tenant_id or "default",
                task_type=task_type or "chat",
                objective_hash=_objective_hash(objective),
                objective_preview=_preview(objective),
                asset_bundle_id=bundle.bundle_id if bundle is not None else None,
                asset_bundle=bundle_dict,
                # No bundle at all is the strongest form of "partial": we know
                # nothing about what was pinned.
                bundle_partial=bool(bundle.partial) if bundle is not None else True,
                backfilled=backfilled,
                outcome={**(outcome or {}), "log_counts": counts},
                quality_score=(outcome or {}).get("quality_score"),
                cost_usd=(outcome or {}).get("cost_usd"),
                latency_ms=latency_ms,
                risk_result=(outcome or {}).get("risk_result"),
                privacy_class=privacy_class,
                event_count=event_count,
                started_at=started_at,
                completed_at=completed_at or datetime.now(timezone.utc),
            )
            db.add(episode)
            db.commit()
    except Exception as exc:
        logger.warning("[assembler] episode assembly failed for %s: %s", message_id, exc)
        return None

    attach_episode(message_id, episode_id)
    logger.info(
        "[assembler] episode %s assembled (run=%s events=%d)", episode_id, run_id, event_count
    )
    return episode_id


def is_replay_eligible(episode: EvolutionEpisode) -> bool:
    """Whether an episode may be used for counterfactual replay.

    Backfilled or partially-bound episodes are barred: replay measures the
    effect of substituting *one* asset while everything else is held fixed, and
    that claim is meaningless if we do not know what everything else was. Such
    episodes remain fully usable for pattern mining and attribution-rule tuning.
    """
    if episode is None:
        return False
    if getattr(episode, "backfilled", False):
        return False
    if getattr(episode, "bundle_partial", True):
        return False
    return bool(getattr(episode, "asset_bundle_id", None))


def replay_rejection_reason(episode: EvolutionEpisode) -> str:
    """Human-readable explanation for the console."""
    if episode is None:
        return "episode_not_found"
    if getattr(episode, "backfilled", False):
        return "backfilled_episode_has_no_asset_snapshot"
    if getattr(episode, "bundle_partial", True):
        return "asset_bundle_incomplete"
    if not getattr(episode, "asset_bundle_id", None):
        return "asset_bundle_missing"
    return ""
