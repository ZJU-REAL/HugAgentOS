"""Database package facade.

Leaf modules (engine, utc_datetime, ...) must stay importable without loading
every ORM model: edition model extensions import them while ``core.db.models``
is importing those extensions. Model names resolve lazily on first access.
"""

import importlib
import importlib.util

from core.db.engine import Base, get_db, init_db  # noqa: F401


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    if importlib.util.find_spec(f"{__name__}.{name}") is not None:
        # ``from core.db import <submodule>`` must not drag in every model.
        return importlib.import_module(f"{__name__}.{name}")
    models = importlib.import_module("core.db.models")
    try:
        value = getattr(models, name)
    except AttributeError:
        raise AttributeError(f"module 'core.db' has no attribute {name!r}") from None
    globals()[name] = value
    return value
