# -*- coding: utf-8 -*-
"""Checkpoint replay, trigger decisions, persistence, and LLM orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

from core.chat import inflight
from core.chat.context import build_effective_user_message
from core.config.settings import settings
from core.llm import compaction as C
from core.llm.context_ir import SESSION_CONTEXT_META_KEY
from core.llm.context_manager import (
    AUTO_COMPACT_MAX_RATIO,
    resolve_model_context_window,
    usable_context_window,
)
from core.llm.message_compat import build_replay_dicts, flatten_tool_output

logger = logging.getLogger(__name__)

# Guard only against pathological single tool results during replay.
_CROSS_TURN_TOOL_CHARS = 1_000_000


# ── History replay (checkpoint-aware) ────────────────────────────────────────


_CANCELLED_TURN_MARKER = "[本轮回答被用户中断]"


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


def _normalize_rows(rows: List[Any], live_run_ids: frozenset = frozenset()) -> List[Dict[str, Any]]:
    """Normalize message rows, excluding internal compaction checkpoints.

    A row still being written by a live run is skipped: it is half-written by
    definition and would show the model a truncated version of its own reply.
    """
    out: List[Dict[str, Any]] = []
    for msg in rows:
        role = getattr(msg, "role", None)
        extra = getattr(msg, "extra_data", None) or {}
        if role == "system" and extra.get("kind") == C.COMPACTION_CHECKPOINT_KIND:
            continue
        owner = inflight.marker(extra)
        if owner and owner["run_id"] in live_run_ids:
            continue
        content = getattr(msg, "content", "")
        if role == "user":
            quoted = extra.get("quoted_follow_up")
            row = {
                "role": "user",
                "content": build_effective_user_message(content, quoted),
            }
            if isinstance(extra.get(SESSION_CONTEXT_META_KEY), dict):
                row[SESSION_CONTEXT_META_KEY] = dict(extra[SESSION_CONTEXT_META_KEY])
            out.append(row)
        elif role == "assistant":
            assistant_text = content or ""
            if extra.get("cancelled"):
                # 用户中途喊停的那一轮。不标这一句，模型下一轮看到的只是一段没头
                # 没尾断掉的话，容易当成自己已经说完，于是接着往下讲或者从头重来。
                assistant_text = (
                    f"{assistant_text}\n\n{_CANCELLED_TURN_MARKER}"
                    if assistant_text
                    else _CANCELLED_TURN_MARKER
                )
            out.extend(
                build_replay_dicts(
                    "assistant",
                    assistant_text,
                    getattr(msg, "tool_calls", None),
                    max_args_chars=_CROSS_TURN_TOOL_CHARS,
                    max_result_chars=_CROSS_TURN_TOOL_CHARS,
                )
            )
        else:
            row = {"role": role or "user", "content": content}
            if isinstance(extra.get(SESSION_CONTEXT_META_KEY), dict):
                row[SESSION_CONTEXT_META_KEY] = dict(extra[SESSION_CONTEXT_META_KEY])
            out.append(row)
    return out


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
    ckpt = (
        chat_service.get_latest_compaction_checkpoint(chat_id)
        if settings.compaction.enabled
        else None
    )
    if ckpt is None:
        rows = _rows_after_seq(chat_service, chat_id, 0, repair=repair)
        return _normalize_rows(rows, _live_run_ids(chat_service, rows))

    extra = getattr(ckpt, "extra_data", None) or {}
    replacement: List[Dict[str, Any]] = list(extra.get("replacement_history") or [])
    covered_seq = chat_service._checkpoint_covered_seq(ckpt)
    tail_rows = _rows_after_seq(chat_service, chat_id, covered_seq, repair=repair)
    return replacement + _normalize_rows(tail_rows, _live_run_ids(chat_service, tail_rows))


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
    fallback = settings.compaction.trigger_ratio
    try:
        from core.services.system_config import SystemConfigService

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
    cfg = settings.compaction
    limit = cfg.token_limit if cfg.token_limit and cfg.token_limit > 0 else None
    if not context_window or context_window <= 0:
        return limit
    if limit is None:
        limit = int(context_window * (cfg.trigger_ratio if ratio is None else ratio))
    return min(limit, int(context_window * AUTO_COMPACT_MAX_RATIO))


def should_compact(active_tokens: Optional[int], limit: Optional[int]) -> bool:
    if not settings.compaction.enabled:
        return False
    if not active_tokens or not limit or limit <= 0:
        return False
    return active_tokens >= limit


def resolve_active_tokens(usage: Optional[Dict[str, Any]]) -> int:
    """Extract the "end-of-turn real context usage" from whole-turn usage, for :func:`should_compact`.

    Prefer ``context_tokens`` (prompt+completion of the last LLM call, see
    ``streaming.get_usage``); when old meta lacks this field, fall back to the
    whole-turn cumulative ``total_tokens`` — tool loops re-accumulate the prompt
    repeatedly, so the cumulative value overestimates usage; better to compact early.
    """
    u = usage or {}
    return int(u.get("context_tokens") or u.get("total_tokens") or 0)


# ── Summary LLM call (mirrors the one-shot httpx call in followups.py) ───────


def _resolve_summarizer_model() -> tuple[str, str, str, str]:
    """Use the main chat model because compaction input may fill its window."""
    try:
        from core.services.model_config import ModelConfigService

        c = ModelConfigService.get_instance().resolve("main_agent")
        if c:
            return c.base_url, c.api_key, c.model_name, c.provider
    except Exception as exc:  # noqa: BLE001
        logger.debug("[compaction] model config unavailable: %s", exc)
    return "", "", "", ""


def _render_content_for_summary(content: Any) -> str:
    """Render one message's content (str or list of blocks) into full text readable by the summary model.

    Difference from :func:`core.llm.compaction._message_text`: tool_call /
    tool_result blocks are **not dropped** — they are rendered as structured text
    carrying tool name + arguments / output. Aligned with Codex — the summary
    model sees the complete history including function_call/output. We don't use
    the native tool_calls message format because different OpenAI-compatible
    endpoints' chat templates vary in how they accept orphan tool messages;
    structured text is information-equivalent and maximally compatible.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    pieces: List[str] = []
    for item in content:
        if isinstance(item, str):
            if item:
                pieces.append(item)
            continue
        if not isinstance(item, dict):
            continue
        btype = item.get("type")
        if btype in (None, "text", "input_text", "output_text"):
            t = item.get("text") or item.get("output") or ""
            if t:
                pieces.append(str(t))
        elif btype in ("tool_call", "tool_use"):
            name = item.get("name") or "unknown_tool"
            args = item.get("input") or ""
            pieces.append(f"[tool_call {name}] arguments: {args}")
        elif btype == "tool_result":
            name = item.get("name") or "unknown_tool"
            out = item.get("output")
            if out is None:
                out = item.get("content", "")
            pieces.append(f"[tool_result {name}]\n{flatten_tool_output(out)}")
    return "\n".join(pieces)


def _flatten_for_summary(history: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Flatten possibly block-containing history into OpenAI /chat/completions [{role, content}].

    Tool calls/results are preserved as structured text via
    :func:`_render_content_for_summary`; role='tool' is mapped to user
    (OpenAI-compatible endpoints only accept user/assistant/system).
    """
    flat: List[Dict[str, str]] = []
    for m in history:
        role = m.get("role", "user")
        text = _render_content_for_summary(m.get("content"))
        if role not in ("user", "assistant", "system"):
            role = "user"
        if not text:
            continue
        flat.append({"role": role, "content": text})
    return flat


def _load_base_system_prompt() -> str:
    """Get the main conversation's base system prompt (without the tools appendix or other runtime context).

    Aligned with Codex, whose summary request carries base_instructions: it lets
    the summary model understand the assistant's role positioning and behavioral
    constraints, so the handoff summary stays consistent with the main
    conversation. Returns an empty string when unavailable (degrade to omitting it).
    """
    try:
        from prompts.prompt_config import load_prompt_config
        from prompts.prompt_runtime import build_system_prompt

        return build_system_prompt(load_prompt_config()) or ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("[compaction] base system prompt unavailable: %s", exc)
        return ""


# Max retries for context-overflow self-rescue (each retry drops a proportional
# slice of the oldest history; converges quickly)
_SUMMARIZE_MAX_ATTEMPTS = 5

# Typical keywords of "context exceeded" errors from OpenAI-compatible endpoints
# (400/413 response body, lowercase matching)
_CTX_ERROR_MARKERS = (
    "context length",
    "context_length",
    "maximum context",
    "context window",
    "too long",
    "token limit",
    "tokens exceed",
)


def _looks_like_context_error(status_code: int, body: str) -> bool:
    if status_code == 413:
        return True
    if status_code != 400:
        return False
    lowered = (body or "").lower()
    return any(m in lowered for m in _CTX_ERROR_MARKERS)


def _estimate_flat_tokens(messages: List[Dict[str, str]]) -> int:
    return sum(C.approx_token_count(m.get("content") or "") for m in messages)


async def _summarize(history: List[Dict[str, Any]], *, timeout: int) -> Optional[str]:
    """Send history + SUMMARIZATION_PROMPT to the model; return the summary body (None on failure, never raises).

    Context-overflow self-rescue (aligned with Codex ``run_compact_task``'s
    ContextWindowExceeded handling):
    1. Pre-trim: when the summary model's window is known, first trim the input
       into 0.9×window by byte estimation (dropping the oldest history
       messages), saving the API round-trip that would inevitably fail;
    2. Reactive: if a "context exceeded" error still hits (400/413 + keywords)
       → drop a proportional slice of the oldest history and retry, at most
       :data:`_SUMMARIZE_MAX_ATTEMPTS` times.
    The leading system message (base prompt) and the trailing summarization
    instruction are never dropped.
    """
    resolved = _resolve_summarizer_model()
    url, key, model = resolved[:3]
    provider = resolved[3] if len(resolved) > 3 else "unknown"
    if not url or not key or not model:
        logger.warning("[compaction] no summarizer model resolved")
        return None

    messages = _flatten_for_summary(history)
    base_prompt = _load_base_system_prompt()
    if base_prompt:
        # Qwen-family models require system to be only at index 0; the flattened history contains no system rows
        messages.insert(0, {"role": "system", "content": base_prompt})
    messages.append({"role": "user", "content": C.SUMMARIZATION_PROMPT})

    # Droppable range = [droppable_start, len-1): keep the system head and the trailing summarization instruction
    droppable_start = 1 if base_prompt else 0

    # (1) Pre-trim (when the window is known)
    try:
        window = resolve_model_context_window(model)
    except Exception:  # noqa: BLE001
        window = None
    if window and window > 0:
        budget = usable_context_window(window)
        dropped = 0
        while len(messages) - droppable_start > 2 and _estimate_flat_tokens(messages) > budget:
            messages.pop(droppable_start)
            dropped += 1
        if dropped:
            logger.info(
                "[compaction] summarizer input pre-trimmed: dropped %d oldest (budget=%d)",
                dropped,
                budget,
            )

    req: dict = {"model": model, "messages": messages, "temperature": 0.3}
    if any(k in model.lower() for k in ("deepseek", "r1", "qwen")):
        req["chat_template_kwargs"] = {"enable_thinking": False}

    # (2) Call + reactive context-overflow self-rescue. The optional context is
    # bound by CompactionCoordinator so each direct HTTP try becomes one
    # append-only model attempt without changing this helper's public signature.
    from core.llm.model_usage import CURRENT_MODEL_USAGE

    usage_context = CURRENT_MODEL_USAGE.get()
    previous_attempt_seq = None

    async def record_attempt(resp=None, *, status: str, started: float) -> None:
        nonlocal previous_attempt_seq
        if usage_context is None:
            return
        from core.harness.usage import AttemptUsage, UsageAttempt, record_usage_safely

        payload = resp.json() if resp is not None and resp.status_code == 200 else {}
        usage = payload.get("usage") if isinstance(payload, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else {}
        try:
            recorded = await record_usage_safely(
                usage_context.recorder,
                UsageAttempt(
                    run_id=usage_context.run_id,
                    kind="model",
                    operation_name=model,
                    provider=provider or "unknown",
                    model=model,
                    status=status,
                    retry_of=previous_attempt_seq,
                    latency_ms=int((time.monotonic() - started) * 1_000),
                    usage=AttemptUsage(
                        prompt_tokens=int(usage.get("prompt_tokens") or 0),
                        completion_tokens=int(usage.get("completion_tokens") or 0),
                        cache_read_tokens=int(
                            usage.get("cache_read_tokens") or details.get("cached_tokens") or 0
                        ),
                        cache_write_tokens=int(usage.get("cache_write_tokens") or 0),
                    ),
                    metadata={
                        "source": "compaction",
                        "http_status": getattr(resp, "status_code", None),
                    },
                ),
            )
            previous_attempt_seq = recorded.attempt_seq
        except Exception:  # noqa: BLE001
            pass

    for attempt in range(_SUMMARIZE_MAX_ATTEMPTS):
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json=req,
                )
        except asyncio.CancelledError:
            await record_attempt(status="cancelled", started=started)
            raise
        except Exception as exc:  # noqa: BLE001
            from core.harness.usage import attempt_status_for_exception

            await record_attempt(status=attempt_status_for_exception(exc), started=started)
            logger.warning("[compaction] summarize failed: %r", exc)
            return None

        if resp.status_code == 200:
            await record_attempt(resp, status="success", started=started)
            raw = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "") or ""
            from core.llm.message_compat import strip_thinking

            summary = strip_thinking(raw).strip()
            return summary or None

        body = resp.text[:500]
        await record_attempt(resp, status="failed", started=started)
        droppable = (
            len(messages) - droppable_start - 1
        )  # trailing summarization instruction excluded
        if _looks_like_context_error(resp.status_code, body) and droppable > 1:
            drop_n = max(1, droppable // 5)
            del messages[droppable_start : droppable_start + drop_n]
            req["messages"] = messages
            logger.info(
                "[compaction] summarizer context exceeded, dropped %d oldest, retry %d/%d",
                drop_n,
                attempt + 1,
                _SUMMARIZE_MAX_ATTEMPTS,
            )
            continue

        logger.warning("[compaction] summarizer API %s: %s", resp.status_code, body[:200])
        return None

    logger.warning(
        "[compaction] summarizer still over context after %d trims",
        _SUMMARIZE_MAX_ATTEMPTS,
    )
    return None


class CompactionCoordinator:
    """Transaction coordinator shared by every compaction phase.

    The database session is deliberately short-lived on both sides of the LLM
    call: first acquire a source watermark + lease, then summarize without a DB
    connection, finally publish through a version/lease CAS.
    """

    def __init__(self, chat_id: str, *, run_id: str = "", hook_bus: Any = None):
        self.chat_id = chat_id
        self.run_id = run_id
        self.owner = f"compactor:{uuid.uuid4().hex}"
        if hook_bus is not None:
            self.hook_bus = hook_bus
        elif run_id:
            from core.harness.events import EventSink
            from core.harness.hooks import HookBus
            from core.services.harness_ledger import (
                DurableEventStore,
                HarnessUsageLedger,
            )

            self.hook_bus = HookBus(
                event_sink=EventSink(DurableEventStore()),
                usage_recorder=HarnessUsageLedger(),
            )
        else:
            self.hook_bus = None

    def _acquire(self):
        from core.db.engine import SessionLocal
        from core.services.chat_service import ChatService

        with SessionLocal() as db:
            return ChatService(db).acquire_compaction_snapshot(
                self.chat_id,
                owner=self.owner,
                lease_seconds=max(30, int(settings.compaction.summarize_timeout_s) + 30),
            )

    def _release(self) -> None:
        from core.db.engine import SessionLocal
        from core.services.chat_service import ChatService

        with SessionLocal() as db:
            ChatService(db).release_compaction_lease(self.chat_id, owner=self.owner)

    def _commit(
        self,
        snapshot: Any,
        *,
        summary_text: str,
        replacement: List[Dict[str, Any]],
        manifest: Dict[str, Any],
    ) -> None:
        from core.db.engine import SessionLocal
        from core.services.chat_service import ChatService

        with SessionLocal() as db:
            ChatService(db).commit_compaction_checkpoint(
                snapshot,
                owner=self.owner,
                summary_text=summary_text,
                replacement_history=replacement,
                replacement_manifest=manifest,
            )

    async def compact(
        self,
        *,
        phase: C.CompactionPhase = C.CompactionPhase.POST_TURN,
        in_memory_history: Optional[List[Dict[str, Any]]] = None,
        budget_inputs: Optional[Dict[str, Any]] = None,
        token_limit: Optional[int] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """Run one phase of the shared pipeline under a watermark + lease + CAS.

        ``phase`` does not change the algorithm; it selects which history is
        authoritative and is recorded on the checkpoint so a summary can be
        attributed to where the decision was taken.

        ``in_memory_history`` is only consulted for mid-turn compaction, where
        the agent may contain messages not yet visible in the DB snapshot.
        """
        try:
            snapshot = await asyncio.to_thread(self._acquire)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[compaction] snapshot acquisition failed chat=%s: %r",
                self.chat_id,
                exc,
            )
            return None
        if snapshot is None:
            # A live lease means another compactor owns the checkpoint. Mid-turn
            # compaction is overflow protection for the turn already running, so
            # it still summarizes and hands the result back for immediate use —
            # it just does not publish. Letting the context blow past the window
            # would be the worse failure.
            if phase is C.CompactionPhase.MID_TURN and in_memory_history:
                logger.info(
                    "[compaction] lease busy, mid-turn falls back to ephemeral chat=%s",
                    self.chat_id,
                )
                return await _summarize_without_checkpoint(list(in_memory_history))
            return None

        persisted = list(snapshot.replacement_history) + _normalize_rows(list(snapshot.source_rows))
        # Summarize what the turn actually holds, but publish against the
        # snapshot's watermark so the lease/CAS guarantees are unchanged.
        history = (
            list(in_memory_history)
            if phase is C.CompactionPhase.MID_TURN and in_memory_history
            else persisted
        )
        published = False
        try:
            if not history:
                return None
            inputs = dict(budget_inputs or {})
            if self.hook_bus is not None:
                from core.harness.events import thaw_value
                from core.harness.hooks import HookStage, Invocation

                before = await self.hook_bus.enforce(
                    Invocation.create(
                        run_id=self.run_id,
                        stage=HookStage.BEFORE_COMPACTION,
                        operation_name="context_compaction",
                        data={"history": history, "budget": inputs},
                    )
                )
                if before.data.get("history") is not None:
                    history = thaw_value(before.data["history"])
                if before.data.get("budget") is not None:
                    inputs = thaw_value(before.data["budget"])
            inputs["messages"] = history
            if "system_prompt" not in inputs:
                inputs["system_prompt"] = _load_base_system_prompt()
            estimate = estimate_context_budget(**inputs)
            if token_limit is not None and not should_compact(
                estimate["total_estimated_tokens"], token_limit
            ):
                return None

            cfg = settings.compaction
            from core.llm.model_usage import model_usage_scope

            with model_usage_scope(
                self.run_id,
                self.hook_bus.usage_recorder if self.hook_bus is not None else None,
            ):
                summary = await _summarize(history, timeout=cfg.summarize_timeout_s)
            if not summary:
                return None

            summary_text = C.format_summary_text(summary)
            replacement = C.build_compacted_history(
                C.collect_user_messages(history),
                summary_text,
                max_tokens=cfg.recent_user_max_tokens,
            )
            if self.hook_bus is not None:
                from core.harness.events import thaw_value
                from core.harness.hooks import HookStage, Invocation

                after = await self.hook_bus.enforce(
                    Invocation.create(
                        run_id=self.run_id,
                        stage=HookStage.AFTER_COMPACTION,
                        operation_name="context_compaction",
                        data={"replacement": replacement, "metadata": {}},
                    )
                )
                if after.data.get("replacement") is not None:
                    replacement = thaw_value(after.data["replacement"])
            manifest: Dict[str, Any] = {
                "phase": phase.value,
                "source_high_watermark_seq": snapshot.covered_seq,
                "source_hash": snapshot.source_hash,
                "base_checkpoint_version": snapshot.base_checkpoint_version,
                "budget_estimate": estimate,
            }
            replacement_inputs = {**inputs, "messages": replacement}
            replacement_estimate = estimate_context_budget(**replacement_inputs)
            from core.llm.context_usage import build_compaction_context_usage

            manifest["replacement_budget_estimate"] = replacement_estimate
            manifest["replacement_context_usage"] = build_compaction_context_usage(
                replacement_estimate,
                context_window=int(inputs.get("context_window") or 0),
                model_name=str(inputs.get("model_name") or ""),
                model_provider_id=str(inputs.get("model_provider_id") or ""),
            )
            await asyncio.to_thread(
                self._commit,
                snapshot,
                summary_text=summary_text,
                replacement=replacement,
                manifest=manifest,
            )
            published = True
        except Exception as exc:  # noqa: BLE001
            from core.harness.hooks import HookPaused

            if isinstance(exc, HookPaused):
                raise
            # Persistence failure is an explicit failure: no replacement is
            # returned to the turn and no success/notice is fabricated.
            logger.warning("[compaction] checkpoint commit failed chat=%s: %r", self.chat_id, exc)
            return None
        finally:
            if not published:
                try:
                    await asyncio.to_thread(self._release)
                except Exception as release_exc:  # noqa: BLE001
                    logger.debug(
                        "[compaction] lease release failed chat=%s: %r",
                        self.chat_id,
                        release_exc,
                    )

        logger.info(
            "[compaction] checkpoint written chat=%s phase=%s covered_seq=%d "
            "msgs=%d→%d summary_len=%d",
            self.chat_id,
            phase.value,
            snapshot.covered_seq,
            len(history),
            len(replacement),
            len(summary_text),
        )
        return replacement


async def _summarize_without_checkpoint(
    history: List[Dict[str, Any]],
) -> Optional[List[Dict[str, Any]]]:
    """Summarize and return a replacement without touching the database.

    Used where there is nothing to publish against — an agent with no persisted
    session (sub-agents, plan mode, connectivity probes) — and where publishing
    is not ours to do because another compactor holds the lease.
    """
    cfg = settings.compaction
    summary = await _summarize(history, timeout=cfg.summarize_timeout_s)
    if not summary:
        return None
    return C.build_compacted_history(
        C.collect_user_messages(history),
        C.format_summary_text(summary),
        max_tokens=cfg.recent_user_max_tokens,
    )


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
    if not settings.compaction.enabled:
        return None
    if not chat_id:
        return await _summarize_without_checkpoint(history)
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
    if not settings.compaction.enabled:
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


# ── Pre-turn compaction ──


def estimate_history_tokens(history: List[Dict[str, Any]]) -> int:
    """Estimate history tokens from rendered text without DB or LLM calls."""
    return sum(
        C.approx_token_count(_render_content_for_summary(m.get("content")))
        for m in history
    )


def estimate_context_budget(
    *,
    system_prompt: str = "",
    tool_schema: Any = None,
    messages: Optional[List[Dict[str, Any]]] = None,
    provider_overhead_tokens: Optional[int] = None,
    context_window: int = 0,
    model_name: str = "",
    model_provider_id: str = "",
) -> Dict[str, int]:
    """Return one auditable estimate shared by trigger decisions and manifests.

    The estimate covers exactly what the request will send — system prompt, tool
    schemas, history and protocol overhead. Reserving the model's ``max_tokens``
    on top of that is deliberately absent: output headroom is carried once, as
    the window share in ``EFFECTIVE_CONTEXT_WINDOW_PERCENT``, so a per-model
    output cap the operator never configured cannot move the trigger line.
    """
    # These identity fields travel with the frozen budget inputs so the
    # post-compaction gauge retains the same model/window attribution. They do
    # not participate in token arithmetic.
    del context_window, model_name, model_provider_id
    history = messages or []
    serialized_tools = (
        json.dumps(tool_schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if tool_schema
        else ""
    )
    system_tokens = C.approx_token_count(system_prompt or "")
    tool_tokens = C.approx_token_count(serialized_tools)
    message_tokens = estimate_history_tokens(history)
    overhead_tokens = (
        max(0, int(provider_overhead_tokens))
        if provider_overhead_tokens is not None
        else 8 + (3 * len(history))
    )
    return {
        "system_prompt_tokens": system_tokens,
        "tool_schema_tokens": tool_tokens,
        "message_tokens": message_tokens,
        "provider_overhead_tokens": overhead_tokens,
        "total_estimated_tokens": (
            system_tokens + tool_tokens + message_tokens + overhead_tokens
        ),
    }


def get_compaction_context_state(chat_service: Any, chat_id: str) -> Optional[Dict[str, Any]]:
    """Return the latest compacted-context baseline for user-facing estimates.

    The visible message history remains complete for reading/export.  This
    small projection tells clients which visible messages the checkpoint has
    replaced and how many estimated tokens its replacement history occupies,
    so a context gauge can count ``replacement + post-checkpoint tail`` instead
    of accumulating the full pre-compaction transcript forever.
    """
    if not settings.compaction.enabled:
        return None

    ckpt = chat_service.get_latest_compaction_checkpoint(chat_id)
    if ckpt is None:
        return None

    extra = getattr(ckpt, "extra_data", None) or {}
    replacement = extra.get("replacement_history")
    if not isinstance(replacement, list):
        replacement = []
    created_at = getattr(ckpt, "created_at", None)
    covered_seq = chat_service._checkpoint_covered_seq(ckpt)
    replacement_manifest = extra.get("replacement_manifest")
    replacement_context_usage = (
        replacement_manifest.get("replacement_context_usage")
        if isinstance(replacement_manifest, dict)
        else None
    )

    # A later model call supersedes this estimate with provider usage. Keep
    # exposing the checkpoint boundary, but attach its token projection only
    # while it still covers the newest measured assistant row.
    if isinstance(replacement_context_usage, dict):
        recent = chat_service.message_repo.list_recent_by_chat(chat_id, limit=50)
        latest_usage_seq = 0
        for row in reversed(recent):
            row_extra = getattr(row, "extra_data", None)
            if isinstance(row_extra, dict) and isinstance(
                row_extra.get("context_usage"), dict
            ):
                latest_usage_seq = int(getattr(row, "chat_seq", 0) or 0)
                break
        if latest_usage_seq > covered_seq:
            replacement_context_usage = None

    state = {
        "checkpoint_id": getattr(ckpt, "message_id", ""),
        "checkpoint_created_at": created_at.isoformat() if created_at is not None else None,
        "covered_through_message_id": extra.get("covers_up_to_message_id"),
        "covered_through_chat_seq": covered_seq,
        "covered_message_count": chat_service.message_repo.count_visible_through_seq(
            chat_id, covered_seq
        ),
        "replacement_tokens": estimate_history_tokens(replacement),
    }
    if isinstance(replacement_context_usage, dict):
        state["context_usage"] = replacement_context_usage
    return state


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
    if not settings.compaction.enabled or not chat_id or not history:
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
        # re-introduced via reply). The checkpoint's canonical form ends with the
        # summary, so returning it directly would cause the summary to be
        # mistakenly popped as this turn's input. Move this turn's user message
        # to after the summary — exactly Codex's post-compaction shape of
        # "summary at the end of history, new input following it".
        in_turn = list(replacement)
        if history[-1].get("role") in ("user", "human"):
            for i in range(len(in_turn) - 1, -1, -1):
                m = in_turn[i]
                if m.get("role") == "user" and not C.is_summary_message(
                    str(m.get("content") or "")
                ):
                    del in_turn[i]
                    break
            in_turn.append({"role": "user", "content": history[-1].get("content")})
        return in_turn, True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[compaction] pre-turn compaction failed chat=%s: %r", chat_id, exc)
        return history, False


def pop_compaction_notice(chat_service: Any, chat_id: str) -> bool:
    """Consume the one-shot pending compaction notice flag."""
    if not settings.compaction.enabled:
        return False
    try:
        ckpt = chat_service.get_latest_compaction_checkpoint(chat_id)
        if ckpt is None or not (getattr(ckpt, "extra_data", None) or {}).get("notice_pending"):
            return False
        return chat_service.update_message_extra_data(ckpt.message_id, {"notice_pending": False})
    except Exception as exc:  # noqa: BLE001
        logger.debug("[compaction] pop notice failed chat=%s: %s", chat_id, exc)
        return False
