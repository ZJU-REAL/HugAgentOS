# -*- coding: utf-8 -*-
"""Session-layer integration for context compaction.

Responsibilities:
1. :func:`load_session_history` — checkpoint-aware history loading (hot path):
   when a latest compaction checkpoint exists → fetch only
   ``replacement_history + messages after it`` from the DB (no more
   load-everything-then-discard); when absent → load everything. Cross-turn
   tool results are **not truncated**.
2. :func:`resolve_token_limit` / :func:`should_compact` / :func:`resolve_active_tokens`
   — trigger decision (threshold, end-of-turn real context-usage metric).
3. :func:`run_post_turn_compaction` (end-of-turn background) and
   :func:`maybe_run_pre_turn_compaction` (pre-turn fallback) — share the
   :func:`_compact_and_write_checkpoint` pipeline:
   history + SUMMARIZATION_PROMPT → summary → compacted history → persist checkpoint.

The pure compaction algorithm lives in :mod:`core.llm.compaction`; this module
only handles the "DB ↔ algorithm" orchestration and the LLM call.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from core.chat.context import build_effective_user_message
from core.config.settings import settings
from core.llm import compaction as C
from core.llm.message_compat import build_replay_dicts, flatten_tool_output

logger = logging.getLogger(__name__)

# Per-tool-result character cap during cross-turn replay. Cross-turn replay does
# **not** truncate aggressively, so use a value large enough (only guards against a
# single oversized artifact blowing up the context in one shot; normal content is
# kept verbatim). Compaction via checkpoints is the real safety net.
_CROSS_TURN_TOOL_CHARS = 1_000_000


# ── History replay (checkpoint-aware) ────────────────────────────────────────


def _normalize_rows(rows: List[Any]) -> List[Dict[str, Any]]:
    """Normalize a list of ChatMessage rows into agent-consumable dicts.

    Semantically equivalent to the original ``_load_session_messages``, except:
    - compaction checkpoint rows (role='system' + kind) are skipped;
    - tool results are **not truncated** (``_CROSS_TURN_TOOL_CHARS``).
    """
    out: List[Dict[str, Any]] = []
    for msg in rows:
        role = getattr(msg, "role", None)
        extra = getattr(msg, "extra_data", None) or {}
        if role == "system" and extra.get("kind") == C.COMPACTION_CHECKPOINT_KIND:
            continue
        content = getattr(msg, "content", "")
        if role == "user":
            quoted = extra.get("quoted_follow_up")
            out.append({"role": "user", "content": build_effective_user_message(content, quoted)})
        elif role == "assistant":
            out.extend(
                build_replay_dicts(
                    "assistant",
                    content or "",
                    getattr(msg, "tool_calls", None),
                    max_args_chars=_CROSS_TURN_TOOL_CHARS,
                    max_result_chars=_CROSS_TURN_TOOL_CHARS,
                )
            )
        else:
            out.append({"role": role or "user", "content": content})
    return out


def _rows_after(chat_service: Any, chat_id: str, after_ts: Any) -> List[Any]:
    """Fetch message rows with ``created_at > after_ts`` (chronological). None after_ts → all rows."""
    from core.db.models import ChatMessage

    q = chat_service.db.query(ChatMessage).filter(ChatMessage.chat_id == chat_id)
    if after_ts is not None:
        q = q.filter(ChatMessage.created_at > after_ts)
    return q.order_by(ChatMessage.created_at).all()


def _load_history(chat_service: Any, chat_id: str) -> List[Dict[str, Any]]:
    """Checkpoint-aware history loading (no access check; internal/background use).

    Latest checkpoint exists → fetch only ``replacement_history + messages after
    it`` from the DB — history rows covered by the checkpoint are **not loaded**
    (compacted sessions are precisely the big ones; loading everything just to
    discard it is pure waste); no checkpoint → load everything.
    """
    ckpt = (
        chat_service.get_latest_compaction_checkpoint(chat_id)
        if settings.compaction.enabled
        else None
    )
    if ckpt is None:
        return _normalize_rows(_rows_after(chat_service, chat_id, None))

    extra = getattr(ckpt, "extra_data", None) or {}
    replacement: List[Dict[str, Any]] = list(extra.get("replacement_history") or [])
    tail_rows = _rows_after(chat_service, chat_id, getattr(ckpt, "created_at", None))
    return replacement + _normalize_rows(tail_rows)


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
    return _load_history(chat_service, chat_id)


# ── Trigger decision ─────────────────────────────────────────────────────────


def resolve_token_limit(context_window: Optional[int]) -> Optional[int]:
    """Resolve the compaction trigger threshold (real prompt tokens).

    Explicit ``CHAT_COMPACT_TOKEN_LIMIT`` config takes precedence; otherwise
    derive it as model window × trigger_ratio. Window undeterminable → None
    (never trigger, conservative).
    """
    cfg = settings.compaction
    if cfg.token_limit and cfg.token_limit > 0:
        return cfg.token_limit
    if context_window and context_window > 0:
        return int(context_window * cfg.trigger_ratio)
    return None


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


def _resolve_summarizer_model() -> tuple[str, str, str]:
    """Resolve the compaction summary model: use only the main chat model (aligned with Codex, which uses the same conversation model).

    No fallback to the summarizer role — that role is positioned as a small model
    for "title summaries + classification", a semantic mismatch; and the trigger
    threshold is computed from the main model's window (0.8 × window), so the
    input is close to a full main-model window and a small-window model would
    inevitably fail with context overflow. If the main model cannot be resolved,
    the conversation itself cannot run either, so a compaction failure is the
    least of our worries (return empty → skip this compaction, retry at the end
    of the next turn).
    """
    try:
        from core.services.model_config import ModelConfigService

        c = ModelConfigService.get_instance().resolve("main_agent")
        if c:
            return c.base_url, c.api_key, c.model_name
    except Exception as exc:  # noqa: BLE001
        logger.debug("[compaction] model config unavailable: %s", exc)
    return "", "", ""


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
    url, key, model = _resolve_summarizer_model()
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
        from core.llm.context_manager import resolve_model_context_window

        window = resolve_model_context_window(model)
    except Exception:  # noqa: BLE001
        window = None
    if window and window > 0:
        budget = int(window * 0.9)
        dropped = 0
        while (
            len(messages) - droppable_start > 2
            and _estimate_flat_tokens(messages) > budget
        ):
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

    # (2) Call + reactive context-overflow self-rescue
    for attempt in range(_SUMMARIZE_MAX_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    f"{url}/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json=req,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[compaction] summarize failed: %r", exc)
            return None

        if resp.status_code == 200:
            raw = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "") or ""
            from core.llm.message_compat import strip_thinking

            summary = strip_thinking(raw).strip()
            return summary or None

        body = resp.text[:500]
        droppable = len(messages) - droppable_start - 1  # trailing summarization instruction excluded
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
        "[compaction] summarizer still over context after %d trims", _SUMMARIZE_MAX_ATTEMPTS
    )
    return None


async def _compact_and_write_checkpoint(
    chat_id: str, history: List[Dict[str, Any]]
) -> Optional[List[Dict[str, Any]]]:
    """Compaction pipeline (shared by post-turn/pre-turn): summary → compacted history → persist checkpoint.

    No DB session is held during the LLM call (summarization takes several
    seconds and must not pin a connection-pool connection); a persistence
    failure is only logged as a warning — the compacted result is still usable
    this turn, and the write will be retried at the end of the next turn.

    Returns:
        The compacted replacement history; None when summarization fails.
    """
    cfg = settings.compaction
    summary = await _summarize(history, timeout=cfg.summarize_timeout_s)
    if not summary:
        return None

    summary_text = C.format_summary_text(summary)
    replacement = C.build_compacted_history(
        C.collect_user_messages(history), summary_text, max_tokens=cfg.recent_user_max_tokens
    )

    def _write() -> None:
        from core.db.engine import SessionLocal
        from core.services.chat_service import ChatService

        with SessionLocal() as db:
            ChatService(db).add_compaction_checkpoint(
                chat_id, summary_text=summary_text, replacement_history=replacement
            )

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[compaction] checkpoint write failed chat=%s: %r", chat_id, exc)

    logger.info(
        "[compaction] checkpoint written chat=%s msgs=%d→%d summary_len=%d",
        chat_id,
        len(history),
        len(replacement),
        len(summary_text),
    )
    return replacement


async def run_post_turn_compaction(chat_id: str) -> bool:
    """Post-turn compaction: generate a summary and persist it as a checkpoint (threshold decision is done by the caller via ``should_compact``).

    Idempotent and safe: every failure is swallowed and returns False, never
    affecting the main conversation. Returns True when the checkpoint was
    written successfully.
    """
    if not settings.compaction.enabled:
        return False

    def _load() -> List[Dict[str, Any]]:
        from core.db.engine import SessionLocal
        from core.services.chat_service import ChatService

        with SessionLocal() as db:
            return _load_history(ChatService(db), chat_id)

    try:
        history = await asyncio.to_thread(_load)
        if not history:
            return False
        return (await _compact_and_write_checkpoint(chat_id, history)) is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[compaction] run_post_turn_compaction failed chat=%s: %r", chat_id, exc)
        return False


# ── PreTurn compaction (pre-turn fallback, aligned with Codex pre-turn compaction) ──


def estimate_history_tokens(history: List[Dict[str, Any]]) -> int:
    """Estimate the total token count of a history slice (pure byte estimation, including tool call/result blocks).

    Only CPU-level utf-8 byte counting — no DB / no LLM calls. When PreTurn does
    not trigger, this single summation is the entire cost, so first-token
    latency is unaffected.
    """
    return sum(
        C.approx_token_count(_render_content_for_summary(m.get("content"))) for m in history
    )


async def maybe_run_pre_turn_compaction(
    chat_id: Optional[str],
    history: List[Dict[str, Any]],
    *,
    model_name: str,
    context_window: Optional[int] = None,
) -> tuple[List[Dict[str, Any]], bool]:
    """Pre-turn compaction fallback: the assembled history already exceeds the threshold → synchronously run the same cross-turn compaction and write a checkpoint.

    Covers scenarios the end-of-turn background compaction cannot reach: the
    previous turn's compaction failed / was skipped, the previous turn's tool
    calls blew up the history, etc. Under normal conditions the post-turn
    compaction shrinks the history first and this function returns via the fast
    path.

    The fast path (threshold not exceeded) has zero external overhead: only the
    byte estimation of :func:`estimate_history_tokens` — no DB access, no LLM
    calls — guaranteeing first-token latency is unaffected.

    Args:
        context_window: model window already resolved by the caller (e.g. the
            workflow layer needs it in the same turn to build
            ContextWindowManager); passing it saves one resolution; None →
            resolved internally.

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
            from core.llm.context_manager import resolve_model_context_window

            context_window = resolve_model_context_window(model_name or "")
        limit = resolve_token_limit(context_window)
        if not should_compact(estimate_history_tokens(history), limit):
            return history, False

        logger.info(
            "[compaction] pre-turn triggered chat=%s model=%s limit=%d", chat_id, model_name, limit
        )
        replacement = await _compact_and_write_checkpoint(chat_id, history)
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
                    del in_turn[i]  # this turn's message inside replacement (the last non-summary user)
                    break
            in_turn.append({"role": "user", "content": history[-1].get("content")})
        return in_turn, True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[compaction] pre-turn compaction failed chat=%s: %r", chat_id, exc)
        return history, False


def pop_compaction_notice(chat_service: Any, chat_id: str) -> bool:
    """Consume the "compaction happened, user not yet notified" flag (one-shot).

    Aligned with Codex's Warning event after compaction completes. Our
    compaction finishes in the background after the stream is closed, so it
    cannot be inserted into the current turn's SSE — defer to the next turn's
    first frame: the executor calls this function after run_started; when it
    returns True, emit one ``compaction_notice`` event and clear the flag in
    place (idempotent, notified only once).
    """
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
