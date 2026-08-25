"""Fail-open Langfuse tracing for chat, model, tool and feedback events.

Langfuse is a projection of the authoritative run and physical-attempt
ledgers. Export failures must never affect an agent answer or tool execution.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from core.config.settings import settings
from core.infra.data_masking import mask_log_data

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_CURRENT_TRACE: ContextVar["ChatTrace | None"] = ContextVar(
    "LANGFUSE_CURRENT_CHAT_TRACE", default=None
)
_configuration_warning_emitted = False


@dataclass
class ChatTrace:
    client: Any
    root: Any
    trace_id: str
    run_id: str
    base_metadata: dict[str, Any] = field(default_factory=dict)
    closed: bool = False
    finished: bool = False


def _sdk() -> tuple[Any, Any]:
    from langfuse import get_client, propagate_attributes

    return get_client(), propagate_attributes


def _client() -> Any | None:
    global _configuration_warning_emitted
    if not settings.langfuse.enabled:
        return None
    if not os.getenv("LANGFUSE_PUBLIC_KEY") or not os.getenv("LANGFUSE_SECRET_KEY"):
        if not _configuration_warning_emitted:
            logger.warning("langfuse_enabled_without_credentials")
            _configuration_warning_emitted = True
        return None
    try:
        return _sdk()[0]
    except Exception:  # noqa: BLE001 - observability is strictly fail-open
        logger.warning("langfuse_client_initialization_failed", exc_info=True)
        return None


def _capture_text(value: Any, *, strip_thinking: bool = False) -> dict[str, Any]:
    text = "" if value is None else str(value)
    if not settings.langfuse.capture_content:
        return {"captured": False, "characters": len(text)}
    if strip_thinking:
        text = _THINK_RE.sub("", text)
    try:
        text = str(mask_log_data(text))
    except Exception:  # noqa: BLE001
        text = "[content masking failed]"
    limit = settings.langfuse.max_content_chars
    return {
        "content": text[:limit],
        "characters": len(text),
        "truncated": len(text) > limit,
    }


def _safe_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in dict(metadata or {}).items():
        normalized = re.sub(r"[^A-Za-z0-9_]", "_", str(key))[:80]
        if not normalized:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[normalized] = value
        else:
            safe[normalized] = str(value)[:200]
    return safe


@contextmanager
def chat_trace_scope(
    *,
    run_id: str,
    chat_id: str,
    user_id: str,
    message_id: str,
    question: str,
    model_name: str | None,
    recovering: bool,
) -> Iterator[ChatTrace | None]:
    """Open one root Agent observation for a user question/answer turn."""
    client = _client()
    if client is None:
        yield None
        return

    stack = contextlib.ExitStack()
    state: ChatTrace | None = None
    token = None
    try:
        _, propagate_attributes = _sdk()
        trace_id = client.create_trace_id(seed=run_id)
        base_metadata = {
            "run_id": run_id,
            "message_id": message_id,
            "model": model_name or "",
            "recovering": bool(recovering),
        }
        stack.enter_context(
            propagate_attributes(
                user_id=str(user_id)[:200],
                session_id=str(chat_id)[:200],
                metadata={
                    "run_id": str(run_id)[:200],
                    "message_id": str(message_id)[:200],
                },
                version=settings.langfuse.release or None,
                tags=["chat", "agent"],
                trace_name="agent-question-answer",
                environment=settings.langfuse.environment,
            )
        )
        root = stack.enter_context(
            client.start_as_current_observation(
                name="agent-question-answer",
                as_type="agent",
                trace_context={"trace_id": trace_id},
                input={"question": _capture_text(question)},
                metadata=base_metadata,
                version=settings.langfuse.release or None,
            )
        )
        state = ChatTrace(
            client=client,
            root=root,
            trace_id=trace_id,
            run_id=run_id,
            base_metadata=base_metadata,
        )
        token = _CURRENT_TRACE.set(state)
    except Exception:  # noqa: BLE001
        logger.warning("langfuse_chat_trace_start_failed", exc_info=True)
        stack.close()
        yield None
        return

    try:
        yield state
    except BaseException as exc:
        _finish_trace_state(
            state,
            status="failed",
            answer="",
            metadata={"exception_type": type(exc).__name__},
        )
        raise
    finally:
        if state is not None and not state.finished:
            _finish_trace_state(state, status="unknown", answer="", metadata={})
        if state is not None:
            state.closed = True
        if token is not None:
            try:
                _CURRENT_TRACE.reset(token)
            except ValueError:
                pass
        with contextlib.suppress(Exception):
            stack.close()


def _finish_trace_state(
    state: ChatTrace | None,
    *,
    status: str,
    answer: str,
    metadata: Mapping[str, Any] | None,
) -> None:
    if state is None or state.closed or state.finished:
        return
    if status == "failed":
        level = "ERROR"
    elif status != "completed":
        level = "WARNING"
    else:
        level = None
    merged = {**state.base_metadata, **_safe_metadata(metadata), "status": status}
    try:
        state.root.update(
            output={"answer": _capture_text(answer, strip_thinking=True)},
            metadata=merged,
            level=level,
            status_message=status if level else None,
        )
        state.finished = True
    except Exception:  # noqa: BLE001
        logger.warning("langfuse_chat_trace_finish_failed", exc_info=True)


def finish_current_chat_trace(
    *, status: str, answer: str, metadata: Mapping[str, Any] | None = None
) -> None:
    """Set root output/status before the surrounding scope closes it."""
    _finish_trace_state(
        _CURRENT_TRACE.get(), status=status, answer=answer, metadata=metadata
    )


def start_attempt_observation(
    *,
    kind: str,
    name: str,
    model: str = "",
    metadata: Mapping[str, Any] | None = None,
    input: Any = None,
) -> Any | None:
    """Start a model/tool child under the current chat root."""
    state = _CURRENT_TRACE.get()
    if state is None or state.closed:
        return None
    as_type = "generation" if kind == "model" else "tool"
    payload = None
    if kind == "tool" and settings.langfuse.capture_tool_io and input is not None:
        try:
            payload = mask_log_data(input)
        except Exception:  # noqa: BLE001
            payload = {"captured": False}
    try:
        kwargs: dict[str, Any] = {
            "name": str(name or "unknown")[:160],
            "as_type": as_type,
            "metadata": _safe_metadata(metadata),
            "version": settings.langfuse.release or None,
        }
        if as_type == "generation":
            kwargs["model"] = str(model or name or "unknown")[:160]
        if payload is not None:
            kwargs["input"] = payload
        return state.root.start_observation(**kwargs)
    except Exception:  # noqa: BLE001
        logger.warning("langfuse_attempt_start_failed", exc_info=True)
        return None


def mark_generation_first_token(observation: Any | None, completion_start_time: Any) -> None:
    if observation is None:
        return
    with contextlib.suppress(Exception):
        observation.update(completion_start_time=completion_start_time)


def finish_attempt_observation(
    observation: Any | None,
    *,
    status: str,
    usage: Any = None,
    output: Any = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    if observation is None:
        return
    if status in {"failed", "timeout"}:
        level = "ERROR"
    elif status == "cancelled":
        level = "WARNING"
    else:
        level = None
    usage_details = None
    if usage is not None:
        usage_details = {
            "input_tokens": max(0, int(getattr(usage, "prompt_tokens", 0) or 0)),
            "output_tokens": max(
                0, int(getattr(usage, "completion_tokens", 0) or 0)
            ),
            "cache_read_tokens": max(
                0, int(getattr(usage, "cache_read_tokens", 0) or 0)
            ),
            "cache_write_tokens": max(
                0, int(getattr(usage, "cache_write_tokens", 0) or 0)
            ),
        }
    output_payload = None
    if settings.langfuse.capture_tool_io and output is not None:
        try:
            output_payload = mask_log_data(output)
        except Exception:  # noqa: BLE001
            output_payload = {"captured": False}
    try:
        observation.update(
            output=output_payload,
            metadata={**_safe_metadata(metadata), "status": status},
            usage_details=usage_details,
            level=level,
            status_message=status if level else None,
        )
    except Exception:  # noqa: BLE001
        logger.warning("langfuse_attempt_update_failed", exc_info=True)
    finally:
        with contextlib.suppress(Exception):
            observation.end()


def record_user_feedback(
    *, run_id: str, message_id: str, rating: str, comment: str | None
) -> None:
    """Project like/dislike onto the deterministic trace as a boolean score."""
    client = _client()
    if client is None or not run_id:
        return
    try:
        trace_id = client.create_trace_id(seed=run_id)
        score_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"langfuse:user-feedback:{run_id}:{message_id}",
            )
        )
        safe_comment = None
        if comment and settings.langfuse.capture_content:
            safe_comment = _capture_text(comment).get("content")
        client.create_score(
            name="user_feedback",
            value=1.0 if rating == "like" else 0.0,
            trace_id=trace_id,
            score_id=score_id,
            data_type="BOOLEAN",
            comment=safe_comment,
            metadata={"rating": rating, "message_id": message_id},
            environment=settings.langfuse.environment,
        )
    except Exception:  # noqa: BLE001
        logger.warning("langfuse_feedback_score_failed", exc_info=True)


def shutdown_langfuse() -> None:
    """Flush the lazy singleton during graceful process shutdown."""
    client = _client()
    if client is None:
        return
    with contextlib.suppress(Exception):
        client.shutdown()
