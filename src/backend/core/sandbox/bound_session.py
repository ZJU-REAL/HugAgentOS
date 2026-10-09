"""Background admissions can use a bound sandbox, never allocate a replacement."""
from contextlib import contextmanager
from contextvars import ContextVar
from .errors import SandboxError

_expected_sandbox_id = ContextVar("expected_sandbox_id", default=None)

@contextmanager
def bind(sandbox_id):
    token = _expected_sandbox_id.set(sandbox_id)
    try:
        yield
    finally:
        _expected_sandbox_id.reset(token)

def require_available(sandbox_id, *, stale=False):
    expected = _expected_sandbox_id.get()
    if expected and (sandbox_id != expected or stale):
        raise SandboxError("Bound sandbox is no longer available")
