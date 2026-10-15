"""Optional feature modules that plug into core through registered hooks.

Core never imports a module's implementation. A module contributes behaviour by
registering hooks from its ``setup()`` at startup; when the module is disabled
it registers nothing and core behaves as if the module were not installed.

Which modules exist is an edition concern (``core.extensions.edition``); whether
an installed module is switched on is the module's own decision, usually an env
flag read inside its ``setup()``.
"""

from __future__ import annotations

from importlib import import_module

from core.extensions.registry import (
    AgentProfile,
    agent_profile,
    channel_routes,
    is_enabled,
    lifecycle_hooks,
    mark_enabled,
    register_agent_profile,
    register_channel_route,
    register_lifecycle,
    register_router,
    register_sandbox_route,
    routers,
    sandbox_route,
)
from core.infra.logging import get_logger

logger = get_logger(__name__)

_loaded = False


def load_extensions() -> None:
    """Import every edition module and let it register itself. Idempotent."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    from core.extensions.edition import EDITION_EXTENSIONS

    for module_name in EDITION_EXTENSIONS:
        module = import_module(module_name)
        if module.setup():
            mark_enabled(module.NAME)
            logger.info("extension_enabled", extension=module.NAME)
        else:
            logger.info("extension_disabled", extension=module.NAME)


__all__ = [
    "AgentProfile",
    "agent_profile",
    "channel_routes",
    "is_enabled",
    "lifecycle_hooks",
    "load_extensions",
    "register_agent_profile",
    "register_channel_route",
    "register_lifecycle",
    "register_router",
    "register_sandbox_route",
    "routers",
    "sandbox_route",
]
