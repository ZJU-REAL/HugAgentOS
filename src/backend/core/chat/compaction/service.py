# -*- coding: utf-8 -*-
"""Checkpoint replay, trigger decisions, persistence, and LLM orchestration."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from core.chat import inflight
from core.config import settings as config_settings
from core.llm.context import compaction as C
from core.models.context_window import AUTO_COMPACT_MAX_RATIO, resolve_model_context_window
from core.llm.context.budget import estimate_context_budget, should_compact
from core.chat.compaction.history import normalize_rows
from core.chat.compaction.summary import _load_base_system_prompt, summarize_without_checkpoint

logger = logging.getLogger(__name__)

# ── History replay (checkpoint-aware) ────────────────────────────────────────


def _live_run_ids(chat_service: Any, rows: List[Any]) -> frozenset:
    """Runs referenced by in-flight markers among ``rows`` that are still live."""
    from core.db.models import ChatRun

    run_ids = {
        m["run_id"]
        for m in (inflight.marker(getattr(msg, "extra_data", None)) for msg in rows)
        if m
    }
    if not run_ids:
        return frozenset()
    return frozenset(
        run_id
        for (run_id,) in chat_service.db.query(ChatRun.run_id)
        .filter(ChatRun.run_id.in_(run_ids), ChatRun.status.in_(("pending", "running")))
        .all()
    )


def _rows_after_seq(
    chat_service: Any, chat_id: str, covered_seq: int, *, repair: bool = True
) -> List[Any]:
    """Fetch rows strictly after a durable sequence watermark.

    ``repair`` runs :meth:`ChatService._ensure_message_sequences`, which takes a
    ``FOR NO KEY UPDATE`` row lock on the parent chat. That is a **write** lock
    on what is otherwise a pure read, so it must never be taken on a session
    that outlives the read. The turn-start history load runs on the
    request-scoped session (``Depends(get_db)``), which FastAPI keeps open until
    the SSE response completes — holding the lock there pins it for the whole
    turn, blocks the run's own ``UPDATE chat_sessions`` when it commits the
    reply, and (because the lease heartbeat then blocks too) deadlocks the run
    against its own stream. Read paths therefore pass ``repair=False``; the
    compaction snapshot, which needs an exact watermark and owns a short-lived
    session, keeps the default.
    """
    from core.db.models import ChatMessage

    if repair:
        chat_service._ensure_message_sequences(chat_id)
    return (
        chat_service.db.query(ChatMessage)
        .filter(
            ChatMessage.chat_id == chat_id,
            ChatMessage.chat_seq > int(covered_seq),
        )
        .order_by(ChatMessage.chat_seq)
        .all()
    )


def _load_history(chat_service: Any, chat_id: str, *, repair: bool = True) -> List[Dict[str, Any]]:
    """Checkpoint-aware history loading (no access check; internal/background use).

    Latest checkpoint exists → fetch only ``replacement_history + messages after
    it`` from the DB — history rows covered by the checkpoint are **not loaded**
    (compacted sessions are precisely the big ones; loading everything just to
    discard it is pure waste); no checkpoint → load everything.

    ``repair`` is forwarded to :func:`_rows_after_seq`; see the warning there
    about taking a row lock on a session that outlives the read.
    """
    from core.sandbox.commands.context import append_active_completions

    ckpt = (
        chat_service.get_latest_compaction_checkpoint(chat_id)
        if config_settings.settings.compaction.enabled
        else None
    )
    if ckpt is None:
        rows = _rows_after_seq(chat_service, chat_id, 0, repair=repair)
        return append_active_completions(
            chat_service.db, chat_id, normalize_rows(rows, _live_run_ids(chat_service, rows)))

    extra = getattr(ckpt, "extra_data", None) or {}
    replacement: List[Dict[str, Any]] = list(extra.get("replacement_history") or [])
    covered_seq = chat_service._checkpoint_covered_seq(ckpt)
    tail_rows = _rows_after_seq(chat_service, chat_id, covered_seq, repair=repair)
    return append_active_completions(chat_service.db, chat_id,
        replacement + normalize_rows(tail_rows, _live_run_ids(chat_service, tail_rows)))


def load_session_history(
    chat_service: Any, chat_id: str, user_id: str
) -> Optional[List[Dict[str, Any]]]:
    """Checkpoint-aware history loading (with access check; replaces "list_all_messages + replay").

    Returns:
        Sequence of message dicts; None when the session does not exist or access
        is denied (same semantics as ``list_all_messages``).
    """
    if chat_service.get_session_with_access(chat_id, user_id) is None:
        return None
    # Read-only: never take the sequence-repair row lock here. This runs on the
    # request-scoped session, which stays open for the whole SSE response.
    return _load_history(chat_service, chat_id, repair=False)


# ── Trigger decision ─────────────────────────────────────────────────────────


# Bounds accepted for the admin-console trigger ratio. Outside this range the
# stored value is ignored (a 0.99 ratio leaves no room for the summary call
# itself; a 0.3 ratio compacts a session into uselessness).
_TRIGGER_RATIO_MIN = 0.5
_TRIGGER_RATIO_MAX = AUTO_COMPACT_MAX_RATIO

# Memo of the console-resolved ratio: (value, expires_at_monotonic). Matches
# SystemConfigService's own cache window, so several turns starting at once cost
# one console read rather than one each.
_RATIO_MEMO: Optional[tuple[float, float]] = None
_RATIO_MEMO_TTL_S = 30.0


def resolve_trigger_ratio() -> float:
    """Resolve the single compaction trigger ratio shared by every phase.

    Runtime authority is the Config admin console key
    ``chat.compress_in_turn_ratio`` (DB, effective without a restart); the env
    default lives in :class:`~core.config.settings.CompactionSettings`. Out-of-range
    or unreadable stored values fall back to the env default rather than
    silently disabling compaction.

    ⚠️ **Not for hot paths.** Reading the console goes through
    ``SystemConfigService``, which opens a DB session and seeds missing config
    rows on a cold cache — real I/O, including writes. Callers resolve it once
    where a turn is set up (``agent_factory`` stamps it on the agent) and pass
    the value into :func:`resolve_token_limit`, which stays pure. The short memo
    below keeps a burst of turn setups from repeating that read.
    """
    global _RATIO_MEMO

    memo = _RATIO_MEMO
    now = time.monotonic()
    if memo is not None and memo[1] > now:
        return memo[0]

    value = _read_trigger_ratio()
    _RATIO_MEMO = (value, now + _RATIO_MEMO_TTL_S)
    return value


def _read_trigger_ratio() -> float:
    fallback = config_settings.settings.compaction.trigger_ratio
    try:
        from core.config.store.system_config import SystemConfigService

        raw = (SystemConfigService.get_instance().get("chat.compress_in_turn_ratio") or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[compaction] read chat.compress_in_turn_ratio failed: %s", exc)
        return fallback
    if not raw:
        return fallback
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        logger.warning("[compaction] chat.compress_in_turn_ratio 非法(%s)，忽略", raw)
        return fallback
    if not _TRIGGER_RATIO_MIN <= parsed <= _TRIGGER_RATIO_MAX:
        logger.warning("[compaction] chat.compress_in_turn_ratio 越界(%s)，忽略", raw)
        return fallback
    return parsed


def resolve_token_limit(
    context_window: Optional[int], *, ratio: Optional[float] = None
) -> Optional[int]:
    """Resolve the compaction trigger threshold (real prompt tokens).

    Explicit ``CHAT_COMPACT_TOKEN_LIMIT`` config takes precedence; otherwise
    derive it as model window × trigger_ratio, capped at
    ``AUTO_COMPACT_MAX_RATIO`` of the window. Window undeterminable → None
    (never trigger, conservative) for the derived case, while an explicit limit
    still stands: there is nothing to cap it against.
    """
    cfg = config_settings.settings.compaction
    limit = cfg.token_limit if cfg.token_limit and cfg.token_limit > 0 else None
    if not context_window or context_window <= 0:
        return limit
    if limit is None:
        limit = int(context_window * (cfg.trigger_ratio if ratio is None else ratio))
    return min(limit, int(context_window * AUTO_COMPACT_MAX_RATIO))


def resolve_active_tokens(usage: Optional[Dict[str, Any]]) -> int:
    """Prefer the last model call's tokens over cumulative multi-call usage."""
    u = usage or {}
    return int(u.get("context_tokens") or u.get("total_tokens") or 0)


from core.chat.compaction.coordinator import CompactionCoordinator


async def run_compaction(
    chat_id: str,
    history: Optional[List[Dict[str, Any]]] = None,
    *,
    phase: C.CompactionPhase = C.CompactionPhase.POST_TURN,
    budget_inputs: Optional[Dict[str, Any]] = None,
    token_limit: Optional[int] = None,
    run_id: str = "",
    hook_bus: Any = None,
) -> Optional[List[Dict[str, Any]]]:
    """Summarize, build replacement history, and persist a checkpoint.

    Args:
        history: Live history used only by the mid-turn phase. Other phases use
            the authoritative watermarked snapshot.

    Returns:
        The compacted replacement history; None when summarization or the
        checkpoint publish fails.
    """
    return await CompactionCoordinator(chat_id, run_id=run_id, hook_bus=hook_bus).compact(
        phase=phase,
        in_memory_history=history,
        budget_inputs=budget_inputs,
        token_limit=token_limit,
    )


async def run_mid_turn_compaction(
    chat_id: str, history: List[Dict[str, Any]]
) -> Optional[List[Dict[str, Any]]]:
    """Run the shared pipeline for a ReAct step-boundary trigger.

    ``chat_id`` may be empty for agents that have no persisted session
    (sub-agents, plan mode, connectivity probes): the summary is still produced
    and returned, only the checkpoint write is skipped.
    """
    if not config_settings.settings.compaction.enabled:
        return None
    if not chat_id:
        return await summarize_without_checkpoint(history)
    return await run_compaction(chat_id, history, phase=C.CompactionPhase.MID_TURN)


async def run_post_turn_compaction(
    chat_id: str,
    *,
    budget_inputs: Optional[Dict[str, Any]] = None,
    token_limit: Optional[int] = None,
    run_id: str = "",
) -> bool:
    """Post-turn compaction: estimate the authoritative snapshot, then compact.

    When ``token_limit`` is supplied, the shared Coordinator applies the same
    component estimator used by pre-turn compaction before calling the summary
    model. Idempotent and safe: every failure is swallowed and returns False,
    never affecting the main conversation. Returns True only after commit.
    """
    if not config_settings.settings.compaction.enabled:
        return False

    try:
        return (
            await run_compaction(
                chat_id,
                phase=C.CompactionPhase.POST_TURN,
                budget_inputs=budget_inputs,
                token_limit=token_limit,
                run_id=run_id,
            )
        ) is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[compaction] run_post_turn_compaction failed chat=%s: %r", chat_id, exc)
        return False


async def maybe_run_pre_turn_compaction(
    chat_id: Optional[str],
    history: List[Dict[str, Any]],
    *,
    model_name: str,
    context_window: Optional[int] = None,
    system_prompt: Optional[str] = None,
    tool_schema: Any = None,
    provider_overhead_tokens: Optional[int] = None,
    run_id: str = "",
) -> tuple[List[Dict[str, Any]], bool]:
    """Synchronously compact history that is already over budget before a turn.

    Covers scenarios the end-of-turn background compaction cannot reach: the
    previous turn's compaction failed / was skipped, the previous turn's tool
    calls blew up the history, etc. Under normal conditions the post-turn
    compaction shrinks the history first and this function returns via the fast
    path.

    Args:
        context_window: model window already resolved by the caller (the
            workflow layer reads it off the model object in the same turn);
            passing it saves one resolution; None → resolved internally.

    Returns:
        ``(history, compacted)``: on trigger-and-success, returns the compacted
        history and True; otherwise returns the input unchanged. Every failure
        is swallowed (later trim acts as the safety net), never affecting the
        main conversation.
    """
    if not config_settings.settings.compaction.enabled or not chat_id or not history:
        return history, False

    try:
        if context_window is None:
            context_window = resolve_model_context_window(model_name or "")
        limit = resolve_token_limit(context_window, ratio=resolve_trigger_ratio())
        effective_system_prompt = (
            _load_base_system_prompt() if system_prompt is None else system_prompt
        )
        budget_estimate = estimate_context_budget(
            system_prompt=effective_system_prompt,
            tool_schema=tool_schema,
            messages=history,
            provider_overhead_tokens=provider_overhead_tokens,
        )
        if not should_compact(budget_estimate["total_estimated_tokens"], limit):
            return history, False

        logger.info(
            "[compaction] pre-turn triggered chat=%s model=%s limit=%d",
            chat_id,
            model_name,
            limit,
        )
        replacement = await run_compaction(
            chat_id,
            phase=C.CompactionPhase.PRE_TURN,
            budget_inputs={
                "system_prompt": effective_system_prompt,
                "tool_schema": tool_schema,
                "provider_overhead_tokens": provider_overhead_tokens,
            },
            token_limit=limit,
            run_id=run_id,
        )
        if replacement is None:
            return history, False

        # In-turn consumption view: both the stream and reply paths follow the
        # convention "last user message = this turn's input" (popped, then
        # re-introduced via reply). The replacement keeps the recent tail
        # verbatim, so the current instruction is already its last row unless
        # the whole tail was summarized — then it is re-appended after the
        # summary, and the summary itself is never mistaken for the input.
        in_turn = list(replacement)
        current = history[-1]
        if current.get("role") in ("user", "human"):
            last = in_turn[-1] if in_turn else {}
            if not (
                last.get("role") in ("user", "human")
                and last.get("content") == current.get("content")
                and not C.is_summary_message(str(last.get("content") or ""))
            ):
                in_turn.append({"role": "user", "content": current.get("content")})
        return in_turn, True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[compaction] pre-turn compaction failed chat=%s: %r", chat_id, exc)
        return history, False


def pop_compaction_notice(chat_service: Any, chat_id: str) -> bool:
    """Consume the one-shot pending compaction notice flag."""
    if not config_settings.settings.compaction.enabled:
        return False
    try:
        ckpt = chat_service.get_latest_compaction_checkpoint(chat_id)
        if ckpt is None or not (getattr(ckpt, "extra_data", None) or {}).get("notice_pending"):
            return False
        return chat_service.update_message_extra_data(ckpt.message_id, {"notice_pending": False})
    except Exception as exc:  # noqa: BLE001
        logger.debug("[compaction] pop notice failed chat=%s: %s", chat_id, exc)
        return False
