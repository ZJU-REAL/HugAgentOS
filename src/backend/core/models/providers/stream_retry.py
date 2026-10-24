"""Bounded retries before the first delivered model event."""

from __future__ import annotations

import logging
from datetime import datetime
from time import monotonic as _monotonic
from time import perf_counter as _perf_counter

from core.models.admission import check_request_admission

logger = logging.getLogger(__name__)


def _is_retryable_stream_start_error(exc: BaseException) -> bool:
    """Whether a failure *before the first stream event* may be answered by one more attempt.

    Classified by exception type, never by message text: transport drops,
    timeouts, 429 and 5xx are transient by definition, and a bare ``APIError``
    is how the OpenAI client surfaces an error event the provider put inside the
    SSE stream. Every other status error describes the request itself (auth,
    bad request, not found) and repeating it would only repeat the answer.
    """
    import openai

    if isinstance(
        exc,
        (openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError),
    ):
        return True
    if isinstance(exc, openai.APIStatusError):
        return False
    return type(exc) is openai.APIError


async def _stream_with_bounded_retry(
    model,
    *,
    reissue,
    parse,
    model_name: str,
    start_datetime: datetime,
    response,
    request_started: float,
):
    """Yield the parsed stream; re-issue the request once if it fails before its first event.

    ``reissue`` re-sends the identical request and ``parse`` turns a raw stream into
    ``ChatResponse`` objects; both are supplied by the caller so the two wire protocols
    (``/chat/completions`` and ``/responses``) share one retry/telemetry path instead of
    keeping two copies of it.

    A streaming completion is a read-only call and stays idempotent until the
    first chunk has been consumed, so exactly one more attempt is made at that
    point (bounded retry, see the capability plan's call rules). A failure after
    the first chunk, or one that is not transient, propagates unchanged — the
    run then ends with a structured error rather than a fabricated reply.
    """
    gen = parse(start_datetime, response)
    _t_wait = _perf_counter()
    try:
        first = await gen.__anext__()
    except StopAsyncIteration:
        return
    except Exception as exc:  # noqa: BLE001 - classified below
        if not _is_retryable_stream_start_error(exc):
            raise
        from core.models.usage import note_provider_retry_started, record_provider_failure

        await record_provider_failure(
            model,
            model_name,
            exc,
            started=request_started,
            provider=model.provider_id,
            metadata={"fallback": "stream_start_retry"},
        )
        logger.warning(
            "Model %s stream failed before its first event (%s); retrying once",
            model_name,
            type(exc).__name__,
        )
        retry_started = _monotonic()
        note_provider_retry_started(model, model_name, retry_started)
        try:
            check_request_admission()
            response = await reissue()
            gen = parse(datetime.now(), response)
            _t_wait = _perf_counter()
            first = await gen.__anext__()
        except StopAsyncIteration:
            return
        except Exception as retry_exc:  # noqa: BLE001 - recorded, then surfaced
            await record_provider_failure(
                model,
                model_name,
                retry_exc,
                started=retry_started,
                provider=model.provider_id,
                metadata={"fallback": "stream_start_retry_failed"},
            )
            raise
    # Delivery cadence, not just the first byte. A run behind a relay can answer
    # as fast on its first token and still read slowly, and the difference is
    # invisible from either end alone — cloud and desktop emit the same line, so
    # the same model over the two paths is directly comparable.
    _t_first = _perf_counter()
    _events, _t_prev, _max_gap = 1, _t_first, 0.0
    try:
        yield first
        async for item in gen:
            _now = _perf_counter()
            _max_gap = max(_max_gap, _now - _t_prev)
            _t_prev = _now
            _events += 1
            yield item
    finally:
        _span = _t_prev - _t_first
        logger.info(
            "[wire] stream model=%s first_event=%.0fms events=%d span=%.0fms rate=%.1f/s max_gap=%.0fms",
            model_name,
            (_t_first - _t_wait) * 1000,
            _events,
            _span * 1000,
            (_events - 1) / _span if _span > 0 else 0.0,
            _max_gap * 1000,
        )
