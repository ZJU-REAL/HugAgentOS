"""Summary model calls and bounded context-overflow recovery."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import httpx
from core.llm.context import compaction as C
from core.models.context_window import resolve_model_context_window, usable_context_window

logger = logging.getLogger(__name__)


def _resolve_summarizer_model():
    """Use the main chat model because compaction input may fill its window."""
    from core.models.completions.single_turn import Endpoint

    try:
        from core.models.config import ModelConfigService

        c = ModelConfigService.get_instance().resolve("main_agent")
        if c and c.base_url and c.api_key and c.model_name:
            return Endpoint.from_resolved(c)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[compaction] model config unavailable: %s", exc)
    return None


def _flatten_for_summary(history: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Flatten possibly block-containing history into OpenAI /chat/completions [{role, content}].

    Tool calls/results are preserved as structured text via
    :func:`C.render_content_for_summary`; role='tool' is mapped to user
    (OpenAI-compatible endpoints only accept user/assistant/system).
    """
    flat: List[Dict[str, str]] = []
    for m in history:
        role = m.get("role", "user")
        text = C.render_content_for_summary(m.get("content"))
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
    endpoint = _resolve_summarizer_model()
    if endpoint is None:
        logger.warning("[compaction] no summarizer model resolved")
        return None
    model = endpoint.model_name
    provider = endpoint.provider

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

    # (2) Call + reactive context-overflow self-rescue. The optional context is
    # bound by CompactionCoordinator so each direct HTTP try becomes one
    # append-only model attempt without changing this helper's public signature.
    from core.models.usage import CURRENT_MODEL_USAGE
    from core.models.completions.single_turn import build_request, parse_response

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
        except Exception as fallback_error:  # noqa: BLE001
            from core.infra.failures import record_best_effort_failure
            record_best_effort_failure('core.chat.compaction.summary:record_attempt', fallback_error)

    for attempt in range(_SUMMARIZE_MAX_ATTEMPTS):
        started = time.monotonic()
        # 每轮重挂消息：上下文超限的自救会就地删掉最老的几条。
        endpoint_url, headers, req = build_request(endpoint, messages, temperature=0.3)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(endpoint_url, headers=headers, json=req)
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
            return parse_response(endpoint, resp.json()).text or None

        body = resp.text[:500]
        await record_attempt(resp, status="failed", started=started)
        droppable = (
            len(messages) - droppable_start - 1
        )  # trailing summarization instruction excluded
        if _looks_like_context_error(resp.status_code, body) and droppable > 1:
            drop_n = max(1, droppable // 5)
            del messages[droppable_start : droppable_start + drop_n]
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


async def summarize_without_checkpoint(
    history: List[Dict[str, Any]],
    *,
    chat_id: str = "",
    run_id: str = "",
) -> Optional[List[Dict[str, Any]]]:
    """Summarize and return a replacement without touching the database.

    Used where there is nothing to publish against — an agent with no persisted
    session (sub-agents, plan mode, connectivity probes) — and where publishing
    is not ours to do because another compactor holds the lease.
    """
    from core.config.settings import settings

    cfg = settings.compaction
    older, recent = C.split_history_for_compaction(
        history, keep_recent_tokens=cfg.keep_recent_tokens
    )
    if not older:
        return None
    from core.chat.compaction.progress import finish_progress, start_progress

    progress = await start_progress(chat_id, run_id, "mid_turn") if chat_id else None
    summary = None
    try:
        summary = await _summarize(older, timeout=cfg.summarize_timeout_s)
        if not summary:
            return None
        return C.build_compacted_history(C.format_summary_text(summary), recent)
    finally:
        if progress is not None:
            task = asyncio.current_task()
            await finish_progress(
                progress,
                (
                    "completed"
                    if summary
                    else "interrupted" if task and task.cancelling() else "failed"
                ),
            )
