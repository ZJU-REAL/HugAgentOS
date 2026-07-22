"""Authentication package with lazy compatibility exports.

Importing an edition policy module must not eagerly import ``backend``: the backend
depends on the service package, while service admission policies live below this
package.  Lazy attributes keep the historical ``from core.auth import ...`` API
without recreating that cycle.
"""

from importlib import import_module

_BACKEND_EXPORTS = {"AuthService", "UserContext", "get_current_user", "require_auth"}


def __getattr__(name: str):
    if name in _BACKEND_EXPORTS:
        return getattr(import_module("core.auth.backend"), name)
    raise AttributeError(name)


__all__ = sorted(_BACKEND_EXPORTS)
