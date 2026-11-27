"""One non-agent model call made on the side of a chat turn (intent routing, evaluation gate).

Owns what such calls share: picking the side model (a dedicated role, else the chat's own main
model, always with thinking off), draining a streamed response, the timeout and failure mapping,
and recording the attempt in the run's usage ledger under its own source/role so a surrounding
usage scope does not count it a second time. Callers keep their prompts and result parsing.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

CAUSE_TIMEOUT = "timeout"
CAUSE_MODEL_ERROR = "model_error"
CAUSE_TOOL_CALL = "tool_call"


@dataclass(frozen=True)
class ModelOutcome:
    """A side call's raw result. When ``error`` is set, ``text`` is not to be trusted."""

    text: str = ""
    error: Optional[str] = None  # CAUSE_TIMEOUT / CAUSE_TOOL_CALL / CAUSE_MODEL_ERROR / caller's
    latency_ms: int = 0


def elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def resolve_side_model(role_key: str, provider_id: str = "") -> Tuple[Any, bool]:
    """``(model, dedicated)``: the role's model when assigned, else ``provider_id``'s, else main.

    Always the no-thinking (``fast``) variant. ``dedicated`` tells the caller the model is not the
    chat's own, so its window may be smaller and the request may need trimming.
    """
    from core.db.threads import run_db
    from core.llm.runtime import model_cache
    from core.models.config import ModelConfigService

    resolved = await run_db(ModelConfigService.get_instance().resolve, role_key)
    if resolved is not None:
        return model_cache.get_provider_model(resolved.provider_id, mode="fast"), True
    if provider_id:
        return model_cache.get_provider_model(provider_id, mode="fast"), False
    return model_cache.get_main_model(mode="fast"), False


async def complete_once(model, messages, tools):
    """One model call; a stream is drained to its last chunk."""
    response = await model(messages=messages, tools=tools, tool_choice=None)
    if not inspect.isasyncgen(response):
        return response
    last = completed = None
    async for chunk in response:
        last = chunk
        if getattr(chunk, "is_last", False):
            completed = chunk
    return completed or last


def response_text(response: Any) -> tuple[str, bool]:
    """Text of a model response and whether it called a tool."""
    parts: List[str] = []
    called_tool = False
    for block in list(getattr(response, "content", None) or []):
        kind = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        if kind == "text":
            parts.append(str(getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else "") or ""))
        elif kind == "tool_call":
            called_tool = True
    return "".join(parts), called_tool


async def call_once(model, messages, tools, *, run_id: str, source: str, role: str,
                    timeout_s: float, started: Optional[float] = None) -> ModelOutcome:
    """Run one side call and record it; never raises except on cancellation.

    ``started`` lets the caller include its own request-building time in the latency.
    """
    from core.models.usage import CURRENT_MODEL_USAGE

    started = time.monotonic() if started is None else started
    token = CURRENT_MODEL_USAGE.set(None)
    status, completed, error = "failed", None, CAUSE_MODEL_ERROR
    try:
        completed = await asyncio.wait_for(complete_once(model, messages, tools), timeout=timeout_s)
        status, error = "success", None
    except asyncio.TimeoutError:
        status, error = "timeout", CAUSE_TIMEOUT
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] side model call failed: %s", source, exc)
    finally:
        CURRENT_MODEL_USAGE.reset(token)
        await _record_usage(run_id, model, status, completed, elapsed_ms(started),
                            source=source, role=role)
    latency = elapsed_ms(started)
    if error:
        return ModelOutcome(error=error, latency_ms=latency)
    text, called_tool = response_text(completed)
    if called_tool:
        return ModelOutcome(error=CAUSE_TOOL_CALL, latency_ms=latency)
    return ModelOutcome(text=text, latency_ms=latency)


async def _record_usage(run_id: str, model: Any, status: str, response: Any, latency_ms: int, *,
                        source: str, role: str) -> None:
    if not run_id:
        return
    try:
        from core.harness.ledger import HarnessUsageLedger
        from core.harness.usage import AttemptUsage, UsageAttempt, record_usage_safely
        from core.models.usage import _usage_from_response

        name = str(getattr(model, "model", "") or "unknown")
        await record_usage_safely(
            HarnessUsageLedger(),
            UsageAttempt(
                run_id=run_id,
                kind="model",
                operation_name=name,
                provider=str(getattr(model, "provider_id", "") or getattr(model, "provider", "")
                             or "unknown"),
                model=name,
                status=status,
                latency_ms=latency_ms,
                usage=_usage_from_response(response) if response is not None else AttemptUsage(),
                metadata={"source": source, "role": role},
            ),
        )
    except Exception:  # noqa: BLE001 — accounting must never fail the caller
        logger.debug("[%s] usage recording failed", source, exc_info=True)
