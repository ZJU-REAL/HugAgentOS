"""Run cancellation ports preserve the caller's transaction boundary."""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


@dataclass
class RunCancellationServices:
    cancel: Callable[..., Awaitable[bool]] | None = None
    cancel_in_session: Callable[..., Any] | None = None


@lru_cache(maxsize=1)
def cancellation_services() -> RunCancellationServices:
    return RunCancellationServices()


def register_run_cancellation(cancel: Callable[..., Awaitable[bool]], *,
                              cancel_in_session: Callable[..., Any]) -> None:
    services = cancellation_services()
    services.cancel = cancel
    services.cancel_in_session = cancel_in_session


async def cancel_run(run_id: str, *, user_id: str, stop_terminal_worker: bool = False) -> bool:
    cancel = cancellation_services().cancel
    if cancel is None:
        raise RuntimeError("Run cancellation service is not registered")
    return await cancel(run_id, user_id=user_id, stop_terminal_worker=stop_terminal_worker)


def cancel_run_in_session(db, run, *, reason: str, now) -> None:
    cancel = cancellation_services().cancel_in_session
    if cancel is None:
        raise RuntimeError("Transactional run cancellation service is not registered")
    cancel(db, run, reason=reason, now=now)
