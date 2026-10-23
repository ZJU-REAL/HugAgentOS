"""Task-local request admission callback supplied by the execution layer."""
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_check: ContextVar[Callable[[], None] | None] = ContextVar("model_request_admission", default=None)


@contextmanager
def request_admission_callback(check: Callable[[], None]) -> Iterator[None]:
    token = _check.set(check)
    try:
        yield
    finally:
        _check.reset(token)


def check_request_admission() -> None:
    check = _check.get()
    if check is not None:
        check()
