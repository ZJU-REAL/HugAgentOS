"""Physical-attempt usage contracts shared by models, tools and hooks."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class AttemptUsage:
    """Token counters reported by one physical attempt, never cumulative."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        # Provider cache counters describe subdivisions of the input tokens;
        # adding them again would double count usage and inflate billing.
        return int(self.prompt_tokens) + int(self.completion_tokens)


@dataclass(frozen=True)
class UsageAttempt:
    """One immutable model/tool/hook execution fact."""

    run_id: str
    kind: str
    operation_name: str
    status: str
    latency_ms: int
    provider: str = ""
    model: str = ""
    effect_id: str = ""
    retry_of: int | None = None
    usage: AttemptUsage = field(default_factory=AttemptUsage)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    attempt_seq: int | None = None


class UsageRecorder(Protocol):
    """Minimal recorder seam used by the framework-neutral HookBus."""

    def record_attempt(self, attempt: UsageAttempt) -> UsageAttempt:
        """Append one attempt and return it with its allocated sequence."""


def attempt_status_for_exception(exc: BaseException) -> str:
    """Classify provider/library timeout types without importing each SDK."""
    name = type(exc).__name__.lower()
    return "timeout" if isinstance(exc, TimeoutError) or "timeout" in name else "failed"


async def record_usage_safely(
    recorder: UsageRecorder,
    attempt: UsageAttempt,
    *,
    timeout_ms: int = 1_000,
) -> UsageAttempt | None:
    """Bound observability I/O so it cannot become an execution dependency."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(recorder.record_attempt, attempt),
            timeout=max(1, int(timeout_ms)) / 1_000,
        )
    except Exception:  # noqa: BLE001
        return None


class MemoryUsageRecorder:
    """Thread-safe append-only recorder for unit tests and embedded runtimes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, list[UsageAttempt]] = {}

    def record_attempt(self, attempt: UsageAttempt) -> UsageAttempt:
        with self._lock:
            rows = self._rows.setdefault(attempt.run_id, [])
            recorded = UsageAttempt(
                **{
                    **attempt.__dict__,
                    "attempt_seq": len(rows) + 1,
                }
            )
            rows.append(recorded)
            return recorded

    def attempts(self, run_id: str) -> tuple[UsageAttempt, ...]:
        with self._lock:
            return tuple(self._rows.get(run_id, ()))

    def aggregate(self, run_id: str) -> dict[str, int]:
        rows = self.attempts(run_id)
        return {
            "attempt_count": len(rows),
            "failed_attempts": sum(row.status != "success" for row in rows),
            "prompt_tokens": sum(row.usage.prompt_tokens for row in rows),
            "completion_tokens": sum(row.usage.completion_tokens for row in rows),
            "cache_read_tokens": sum(row.usage.cache_read_tokens for row in rows),
            "cache_write_tokens": sum(row.usage.cache_write_tokens for row in rows),
            "total_tokens": sum(row.usage.total_tokens for row in rows),
        }
