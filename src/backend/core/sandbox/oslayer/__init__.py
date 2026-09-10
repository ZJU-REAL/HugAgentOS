"""OS-level sandbox layer: one policy model, three platform backends.

The layer answers a single question — *given this policy, how do I start this
command confined?* — and answers it the same way on every platform: with a
:class:`~core.sandbox.oslayer.backend.SandboxLaunch`, an argv prefix plus an
environment overlay that the process-spawning boundary applies verbatim.

Three properties are deliberate and worth keeping:

* **Nothing degrades quietly.** A missing runner raises
  :class:`SandboxUnavailableError`; a policy the platform cannot fully enforce
  raises :class:`SandboxUnenforceableError`. There is no code path that returns
  an unconfined launch.
* **The policy is data.** It carries no platform vocabulary, serializes to JSON,
  and is resolved against a :class:`PolicyContext` only at build time.
* **Nothing is imported until it is used.** The Windows launcher runs once per
  command and reaches only ``windows_runtime`` plus the ``ctypes`` bindings, but
  importing any submodule runs this file first. Eagerly re-exporting the whole
  surface here would therefore put the policy model — and ``dataclasses`` and
  ``typing`` behind it — on that per-command path for nothing. Hence the lazy
  ``__getattr__`` below, and hence no ``typing`` import in this file either;
  importing ``typing`` alone costs more than the rest of the launcher's startup.
  Callers wanting precise types can import from the submodule that defines the
  name, which is fully annotated.

The model and all three backends are ports of the Codex sandbox
(https://github.com/openai/codex, Apache-2.0); each module names the upstream
file it follows.
"""

from __future__ import annotations

# Public name → the submodule that defines it. Kept explicit so the exported
# surface is readable here without importing anything to find it out.
_EXPORTS = {
    "SandboxBackend": "backend",
    "SandboxLaunch": "backend",
    "build_launch": "backend",
    "current_platform": "backend",
    "get_platform_backend": "backend",
    "unavailable_reason": "backend",
    "SandboxError": "errors",
    "SandboxPolicyError": "errors",
    "SandboxUnavailableError": "errors",
    "SandboxUnenforceableError": "errors",
    "AccessMode": "policy",
    "FileSystemEntry": "policy",
    "FileSystemKind": "policy",
    "FileSystemPolicy": "policy",
    "NetworkPolicy": "policy",
    "PolicyContext": "policy",
    "SandboxPolicy": "policy",
    "SpecialPath": "policy",
    "build_filesystem_policy": "policy",
    "unrestricted_policy": "policy",
}


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(f".{module_name}", __name__), name)
    globals()[name] = value  # resolved once; later lookups skip this path
    return value


def __dir__() -> list[str]:
    return sorted(_EXPORTS)


__all__ = sorted(_EXPORTS)
