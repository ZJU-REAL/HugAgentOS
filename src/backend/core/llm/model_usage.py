"""Physical model-attempt instrumentation below AgentScope's retry loop."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncGenerator, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.harness.usage import (
    AttemptUsage,
    UsageAttempt,
    UsageRecorder,
    attempt_status_for_exception,
    record_usage_safely,
)


@dataclass(frozen=True)
class ModelUsageContext:
    run_id: str
    recorder: UsageRecorder


CURRENT_MODEL_USAGE: ContextVar[ModelUsageContext | None] = ContextVar(
    "CURRENT_MODEL_USAGE", default=None
)
_RETRY_SEQUENCES: ContextVar[dict[str, int] | None] = ContextVar(
    "MODEL_USAGE_RETRY_SEQUENCES", default=None
)
_RECORDED_FAILURES: ContextVar[frozenset[int]] = ContextVar(
    "MODEL_USAGE_RECORDED_FAILURES", default=frozenset()
)
_ATTEMPT_STARTS: ContextVar[dict[str, float] | None] = ContextVar(
    "MODEL_USAGE_ATTEMPT_STARTS", default=None
)


def _reset(var: ContextVar[Any], token: Any) -> None:
    """Undo one `ContextVar.set` even when teardown lands in a foreign context.

    This scope spans `yield` inside async generators (see the hook adapter's
    `on_reply` / `on_reasoning`). When the event loop's async-generator
    finalizer closes such a generator, `athrow(GeneratorExit)` runs in a
    *different* `Context` than the one that created the tokens, and
    `ContextVar.reset(token)` raises `ValueError`. That context is being
    discarded anyway, so there is nothing left to restore — but letting the
    error escape breaks the teardown chain (the remaining resets never run and
    the run never finalizes). Each var is therefore reset independently.
    """
    try:
        var.reset(token)
    except ValueError:
        pass


@contextmanager
def model_usage_scope(run_id: str, recorder: UsageRecorder | None) -> Iterator[None]:
    """Bind all provider tries in one reasoning operation to a durable run."""
    if not run_id or recorder is None:
        yield
        return
    context_token = CURRENT_MODEL_USAGE.set(ModelUsageContext(run_id, recorder))
    retry_token = _RETRY_SEQUENCES.set({})
    failure_token = _RECORDED_FAILURES.set(frozenset())
    starts_token = _ATTEMPT_STARTS.set({})
    try:
        yield
    finally:
        _reset(_ATTEMPT_STARTS, starts_token)
        _reset(_RECORDED_FAILURES, failure_token)
        _reset(_RETRY_SEQUENCES, retry_token)
        _reset(CURRENT_MODEL_USAGE, context_token)


def _attempt_key(model: Any, model_name: str) -> str:
    return f"{id(model)}:{model_name}"


def _retry_of(key: str) -> int | None:
    return (_RETRY_SEQUENCES.get() or {}).get(key)


def _set_retry(key: str, sequence: int | None) -> None:
    values = dict(_RETRY_SEQUENCES.get() or {})
    if sequence is None:
        values.pop(key, None)
    else:
        values[key] = int(sequence)
    _RETRY_SEQUENCES.set(values)


def note_provider_retry_started(model: Any, model_name: str, started: float) -> None:
    """Tell the outer wrapper when an internally retried request really began."""
    key = _attempt_key(model, str(model_name or getattr(model, "model", "") or "unknown"))
    values = dict(_ATTEMPT_STARTS.get() or {})
    values[key] = float(started)
    _ATTEMPT_STARTS.set(values)


def _attempt_started(key: str, fallback: float) -> float:
    return (_ATTEMPT_STARTS.get() or {}).get(key, fallback)


def _clear_attempt_started(key: str) -> None:
    values = dict(_ATTEMPT_STARTS.get() or {})
    values.pop(key, None)
    _ATTEMPT_STARTS.set(values)


def _mark_failure_recorded(exc: BaseException) -> None:
    _RECORDED_FAILURES.set(_RECORDED_FAILURES.get() | {id(exc)})


def _failure_was_recorded(exc: BaseException) -> bool:
    return id(exc) in _RECORDED_FAILURES.get()


def _read(obj: Any, name: str, default: Any = None) -> Any:
    """Read one field off a usage payload without assuming how it stores it.

    AgentScope 的 `ChatUsage` / `ChatResponse` 继承自 `DictMixin`，它的
    `__getattr__` 就是 `dict.__getitem__`——字段不存在时抛的是 `KeyError`，而
    `getattr(obj, name, default)` 只兜 `AttributeError`，兜不住。这里同时还会
    收到各家 provider 的普通对象和 dict。统一走这个读取器，避免为了记一笔用量
    就把整条流打断。
    """
    try:
        value = getattr(obj, name)
    except (AttributeError, KeyError, TypeError):
        if isinstance(obj, Mapping):
            try:
                value = obj[name]
            except (KeyError, TypeError):
                return default
        else:
            return default
    return default if value is None else value


def _usage_from_response(response: Any) -> AttemptUsage:
    usage = _read(response, "usage")
    if usage is None:
        return AttemptUsage()
    input_details = _read(usage, "input_tokens_details")
    return AttemptUsage(
        prompt_tokens=int(_read(usage, "input_tokens") or _read(usage, "prompt_tokens", 0) or 0),
        completion_tokens=int(
            _read(usage, "output_tokens") or _read(usage, "completion_tokens", 0) or 0
        ),
        cache_read_tokens=int(
            _read(usage, "cache_input_tokens")
            or _read(usage, "cache_read_tokens")
            or _read(input_details, "cached_tokens", 0)
            or 0
        ),
        cache_write_tokens=int(
            _read(usage, "cache_creation_input_tokens")
            or _read(usage, "cache_write_tokens", 0)
            or 0
        ),
    )


async def _record_safely(recorder: UsageRecorder, attempt: UsageAttempt) -> UsageAttempt | None:
    return await record_usage_safely(recorder, attempt)


async def record_provider_failure(
    model: Any,
    model_name: str,
    exc: BaseException,
    *,
    started: float,
    provider: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a provider request rejected inside a model-level fallback.

    Some model adapters catch a failed HTTP request and retry internally. This
    helper records that first physical failure; the outer model wrapper later
    records the successful fallback request with ``retry_of`` pointing here.
    """
    context = CURRENT_MODEL_USAGE.get()
    if context is None:
        return
    operation = str(model_name or getattr(model, "model", "") or "unknown")
    key = _attempt_key(model, operation)
    recorded = await _record_safely(
        context.recorder,
        UsageAttempt(
            run_id=context.run_id,
            kind="model",
            operation_name=operation,
            provider=str(provider or getattr(model, "provider_id", "") or type(model).__name__),
            model=operation,
            status=attempt_status_for_exception(exc),
            retry_of=_retry_of(key),
            latency_ms=int((time.monotonic() - started) * 1_000),
            metadata={"physical_provider_request": True, **(metadata or {})},
        ),
    )
    if recorded is not None:
        _set_retry(key, recorded.attempt_seq)
        _mark_failure_recorded(exc)


async def record_external_model_attempt(
    owner: Any,
    *,
    operation_name: str,
    provider: str,
    status: str,
    started: float,
    usage: AttemptUsage | None = None,
    metadata: dict[str, Any] | None = None,
) -> UsageAttempt | None:
    """Record one direct provider request made outside an AgentScope model.

    ``owner`` keeps retries from the same logical caller in one chain while
    independent concurrent callers retain separate ``retry_of`` sequences.
    """
    context = CURRENT_MODEL_USAGE.get()
    if context is None:
        return None
    operation = str(operation_name or "unknown")
    key = _attempt_key(owner, operation)
    recorded = await _record_safely(
        context.recorder,
        UsageAttempt(
            run_id=context.run_id,
            kind="model",
            operation_name=operation,
            provider=str(provider or "unknown"),
            model=operation,
            status=status,
            retry_of=_retry_of(key),
            latency_ms=int((time.monotonic() - started) * 1_000),
            usage=usage or AttemptUsage(),
            metadata={"physical_provider_request": True, **(metadata or {})},
        ),
    )
    if status in {"failed", "timeout"} and recorded is not None:
        _set_retry(key, recorded.attempt_seq)
    elif status not in {"failed", "timeout"}:
        _set_retry(key, None)
    return recorded


def instrument_model_usage(
    model: Any,
    *,
    provider: str | None = None,
) -> Any:
    """Install one wrapper around the model's physical API method."""
    if getattr(model, "_jx_usage_instrumented", False):
        return model

    provider_name = str(
        provider
        or getattr(model, "provider_id", "")
        or getattr(model, "provider", "")
        or getattr(model, "provider_name", "")
        or type(model).__name__
    )
    original = getattr(model, "_call_api", None)
    if original is None or not callable(original):
        model._jx_usage_instrumented = True
        return model

    async def measured(model_name: str, *args: Any, **kwargs: Any) -> Any:
        context = CURRENT_MODEL_USAGE.get()
        if context is None:
            return await original(model_name, *args, **kwargs)

        started = time.monotonic()
        operation = str(model_name or getattr(model, "model", "") or "unknown")
        key = _attempt_key(model, operation)
        _clear_attempt_started(key)

        from core.observability.langfuse import (
            finish_attempt_observation,
            mark_generation_first_token,
            start_attempt_observation,
        )

        langfuse_observation = start_attempt_observation(
            kind="model",
            name=operation,
            model=operation,
            metadata={"provider": provider_name, "method": "_call_api"},
        )

        async def record(
            status: str,
            usage: AttemptUsage | None = None,
            *,
            stream: bool = False,
        ) -> UsageAttempt | None:
            return await _record_safely(
                context.recorder,
                UsageAttempt(
                    run_id=context.run_id,
                    kind="model",
                    operation_name=operation,
                    provider=provider_name,
                    model=operation,
                    status=status,
                    retry_of=_retry_of(key),
                    latency_ms=int((time.monotonic() - _attempt_started(key, started)) * 1_000),
                    usage=usage or AttemptUsage(),
                    metadata={"method": "_call_api", "stream": stream},
                ),
            )

        try:
            response = await original(model_name, *args, **kwargs)
        except asyncio.CancelledError:
            finish_attempt_observation(
                langfuse_observation,
                status="cancelled",
                metadata={"provider": provider_name},
            )
            await record("cancelled")
            _set_retry(key, None)
            _clear_attempt_started(key)
            raise
        except Exception as exc:
            failure_status = attempt_status_for_exception(exc)
            finish_attempt_observation(
                langfuse_observation,
                status=failure_status,
                metadata={"provider": provider_name},
            )
            if not _failure_was_recorded(exc):
                recorded = await record(failure_status)
                if recorded is not None:
                    _set_retry(key, recorded.attempt_seq)
            raise

        if not inspect.isasyncgen(response):
            response_usage = _usage_from_response(response)
            finish_attempt_observation(
                langfuse_observation,
                status="success",
                usage=response_usage,
                metadata={"provider": provider_name, "stream": False},
            )
            await record("success", response_usage)
            _set_retry(key, None)
            _clear_attempt_started(key)
            return response

        async def measured_stream() -> AsyncGenerator[Any, None]:
            last = None
            status = "cancelled"
            first_token = True
            try:
                async for item in response:
                    if first_token:
                        mark_generation_first_token(
                            langfuse_observation, datetime.now(timezone.utc)
                        )
                        first_token = False
                    last = item
                    yield item
                status = "success"
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception as exc:
                status = attempt_status_for_exception(exc)
                raise
            finally:
                response_usage = _usage_from_response(last)
                finish_attempt_observation(
                    langfuse_observation,
                    status=status,
                    usage=response_usage,
                    metadata={"provider": provider_name, "stream": True},
                )
                recorded = await record(status, response_usage, stream=True)
                if status in {"failed", "timeout"} and recorded is not None:
                    _set_retry(key, recorded.attempt_seq)
                elif status not in {"failed", "timeout"}:
                    _set_retry(key, None)
                    _clear_attempt_started(key)

        return measured_stream()

    model._call_api = measured
    model._jx_usage_instrumented = True
    return model
