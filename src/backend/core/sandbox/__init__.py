"""Sandbox contracts and provider selection, loaded only when requested.

The runner image contains a small subset of this package. Importing its HTTP
service must not load backend settings, databases or cloud SDKs.
"""

_EXPORTS = {
    "ProcessRequest": "protocol",
    "ProcessResult": "protocol",
    "SandboxProvider": "protocol",
    "SandboxCapacityExhausted": "errors",
    "SandboxConnectError": "errors",
    "SandboxError": "errors",
    "SandboxFileTooLargeError": "errors",
    "SandboxTimeoutError": "errors",
    "get_sandbox_provider": "factory",
    "reset_provider_cache": "factory",
}


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = sorted(_EXPORTS)
