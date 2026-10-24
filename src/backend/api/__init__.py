"""API layer for HugAgentOS.

``app`` is resolved on first access: importing a submodule such as
``api.deps`` from a route module must not compose the whole application,
which would import that same, still half-initialised route module.
"""

__all__ = ["app"]


def __getattr__(name):
    if name == "app":
        import importlib

        # Importing the submodule binds ``api.app`` to the module; rebind it to
        # the FastAPI instance, as the former eager ``from .app import app`` did.
        instance = importlib.import_module("api.app").app
        globals()["app"] = instance
        return instance
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
