"""Process-wide registry for long-lived background tasks / schedulers.

The FastAPI app starts several background workers (idle-session reaper,
automation scheduler, distillation scheduler, …) and stores their handles as
module globals. Monitoring code (``features.security_console.console``) needs to
report whether they are running, but it must not import the API app module
(that would be a ``core → api`` upward dependency). The app registers handles
here on startup; monitoring reads them back through this neutral registry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


def register(name: str, handle: Any) -> None:
    """Register (or replace) a named background task/scheduler handle."""
    process_state().REGISTRY[name] = handle


def get(name: str) -> Any:
    """Return the handle registered under ``name`` (or None)."""
    return process_state().REGISTRY.get(name)


def clear() -> None:
    """Drop all registered handles (used on shutdown)."""
    process_state().REGISTRY.clear()


@dataclass
class RuntimeState:
    REGISTRY: Dict[str, Any] = field(default_factory=lambda: {})


_STATE = RuntimeState()


def process_state() -> RuntimeState:
    return _STATE
