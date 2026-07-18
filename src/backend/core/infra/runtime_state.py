"""Process-wide registry for long-lived background tasks / schedulers.

The FastAPI app starts several background workers (idle-session reaper,
automation scheduler, distillation scheduler, …) and stores their handles as
module globals. Monitoring code (``core.services.security_service``) needs to
report whether they are running, but it must not import the API app module
(that would be a ``core → api`` upward dependency). The app registers handles
here on startup; monitoring reads them back through this neutral registry.
"""

from typing import Any, Dict

_REGISTRY: Dict[str, Any] = {}


def register(name: str, handle: Any) -> None:
    """Register (or replace) a named background task/scheduler handle."""
    _REGISTRY[name] = handle


def get(name: str) -> Any:
    """Return the handle registered under ``name`` (or None)."""
    return _REGISTRY.get(name)


def clear() -> None:
    """Drop all registered handles (used on shutdown)."""
    _REGISTRY.clear()
