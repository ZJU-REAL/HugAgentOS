"""Sandbox provider singleton factory.

Selects and caches a provider instance based on ``settings.sandbox.provider``.
Test cases can call ``reset_provider_cache()`` to force the next
``get_sandbox_provider()`` to rebuild.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.config.settings import settings

from .protocol import SandboxProvider

logger = logging.getLogger(__name__)


def _ordinary_provider() -> SandboxProvider:
    if process_state().provider is not None:
        return process_state().provider

    kind = settings.sandbox.provider
    if kind == "script_runner":
        from core.sandbox.providers.script_runner.provider import ScriptRunnerProvider

        process_state().provider = ScriptRunnerProvider()
    elif kind == "opensandbox":
        from core.sandbox.providers.edition import PersistentSandboxProvider

        process_state().provider = PersistentSandboxProvider()
    elif kind == "cube":
        from core.sandbox.providers.cube.provider import CubeSandboxProvider

        process_state().provider = CubeSandboxProvider()
    else:
        raise ValueError(
            f"未知 SANDBOX_PROVIDER: {kind!r}，可选: script_runner / opensandbox / cube"
        )

    logger.info("[sandbox] activated provider=%s", process_state().provider.name)
    return process_state().provider


def get_sandbox_provider() -> SandboxProvider:
    from core.sandbox.sessions.router import SessionSandboxRouter

    return SessionSandboxRouter(_ordinary_provider())


def reset_provider_cache() -> None:
    """For testing use only."""
    process_state().provider = None


@dataclass
class FactoryState:
    provider: SandboxProvider | None = None


_STATE = FactoryState()


def process_state() -> FactoryState:
    return _STATE
