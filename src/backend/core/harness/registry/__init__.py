"""Optional feature modules that plug into core through registered hooks.

Core never imports a module's implementation. A module contributes behaviour by
registering hooks from its ``setup()`` at startup; when the module is disabled
it registers nothing and core behaves as if the module were not installed.

Shared modules are loaded in every edition. Additional modules are an edition concern (``core.harness.registry.edition``); whether
an installed module is switched on is the module's own decision, usually an env
flag read inside its ``setup()``.
"""

from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module

from core.harness.registry.user_cleanup import register_user_cleanup
from core.harness.registry.hook_factories import hook_specs, register_hook_factory

from core.harness.registry.registry import AgentProfile, NativeToolContext, register_tool, install_tools, SandboxCredentials, register_sandbox_credentials, sandbox_credentials, agent_profile, channel_routes, is_enabled, lifecycle_hooks, mark_enabled, register_agent_profile, register_channel_route, register_lifecycle, register_router, register_sandbox_route, routers, sandbox_route
from core.infra.logging import get_logger

logger = get_logger(__name__)

_loaded = False


def load_models(installed: Iterable[str]) -> None:
    """Register installed schemas independently of runtime enablement."""
    for package in installed:
        import_module(f"{package}.models")


def load_extensions(installed: Iterable[str]) -> None:
    """Import shared and edition modules and let it register itself. Idempotent."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    from core.harness.registry.edition import EDITION_EXTENSIONS

    installed = tuple(installed)
    load_models(installed)
    from core.harness.registry.schema_lifecycle import load_schema_lifecycle
    load_schema_lifecycle(installed)
    for module_name in (*(f"{name}.extension" for name in installed), *EDITION_EXTENSIONS):
        module = import_module(module_name)
        if module.setup():
            mark_enabled(module.NAME)
            logger.info("extension_enabled", extension=module.NAME)
        else:
            logger.info("extension_disabled", extension=module.NAME)


__all__ = [
    "register_user_cleanup",
    "hook_specs",
    "register_hook_factory",
    "AgentProfile",
    "NativeToolContext",
    "register_tool",
    "install_tools",
    "SandboxCredentials",
    "register_sandbox_credentials",
    "sandbox_credentials",
    "agent_profile",
    "channel_routes",
    "is_enabled",
    "lifecycle_hooks",
    "load_extensions",
    "load_models",
    "register_agent_profile",
    "register_channel_route",
    "register_lifecycle",
    "register_router",
    "register_sandbox_route",
    "routers",
    "sandbox_route",
]
