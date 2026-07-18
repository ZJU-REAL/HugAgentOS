"""Sandbox provider singleton factory.

Selects and caches a provider instance based on ``settings.sandbox.provider``.
Test cases can call ``reset_provider_cache()`` to force the next
``get_sandbox_provider()`` to rebuild.
"""

from __future__ import annotations

import logging

from core.config.settings import settings

from .protocol import SandboxProvider

logger = logging.getLogger(__name__)

_provider: SandboxProvider | None = None


def get_sandbox_provider() -> SandboxProvider:
    global _provider
    if _provider is not None:
        return _provider

    kind = settings.sandbox.provider
    if kind == "script_runner":
        from .script_runner_provider import ScriptRunnerProvider
        _provider = ScriptRunnerProvider()
    elif kind == "opensandbox":
        # Seam C5: when the persistent sandbox module is missing (CE tree), warn clearly and fall back to the lightweight sandbox
        try:
            from .opensandbox_provider import OpenSandboxProvider
            _provider = OpenSandboxProvider()
        except ModuleNotFoundError:
            logger.warning(
                "[sandbox] opensandbox provider 不可用（本发行版未携带），回退 script_runner"
            )
            from .script_runner_provider import ScriptRunnerProvider
            _provider = ScriptRunnerProvider()
    elif kind == "cube":
        try:
            from .cube_provider import CubeSandboxProvider
            _provider = CubeSandboxProvider()
        except ModuleNotFoundError:
            logger.warning(
                "[sandbox] cube provider 不可用（本发行版未携带），回退 script_runner"
            )
            from .script_runner_provider import ScriptRunnerProvider
            _provider = ScriptRunnerProvider()
    else:
        raise ValueError(
            f"未知 SANDBOX_PROVIDER: {kind!r}，可选: script_runner / opensandbox / cube"
        )

    logger.info("[sandbox] activated provider=%s", _provider.name)
    return _provider


def reset_provider_cache() -> None:
    """For testing use only."""
    global _provider
    _provider = None
