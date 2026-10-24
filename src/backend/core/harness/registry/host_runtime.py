"""Host policy for operations that must be forwarded to a remote execution plane."""
from collections.abc import Callable

_remote_execution_required: Callable[[], bool] = lambda: False


def register_remote_execution_policy(predicate: Callable[[], bool]) -> None:
    global _remote_execution_required
    _remote_execution_required = predicate


def requires_remote_execution() -> bool:
    return bool(_remote_execution_required())
