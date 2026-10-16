"""Hook tables that optional modules fill in at startup.

Every table starts empty. Core consults them at its extension points (agent
assembly, sandbox routing, inbound channel messages, app lifecycle, routers),
so an unregistered module leaves those code paths exactly as they were.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional


@dataclass(frozen=True)
class AgentProfile:
    """A dedicated agent built by a module instead of the ordinary chat assembly.

    Selected by ``AgentRequest.profile``; ``AgentRequest.profile_ref`` carries the
    module's own identifier (for example the task the agent works on).
    """

    # (request) -> system prompt; replaces the whole chat prompt assembly.
    system_prompt: Callable[[Any], Awaitable[str]]
    # (toolkit, request, loader=...) -> None; replaces the native chat tools.
    register_tools: Callable[..., None]
    # (request) -> extra policy middlewares appended to the stack.
    middlewares: Callable[[Any], list] = field(default=lambda request: [])
    # Only function tools whose name passes are exposed; None keeps all.
    tool_filter: Optional[Callable[[str], bool]] = None
    # False: MCP servers come only from the request's explicit list, never catalog defaults.
    inherit_default_mcps: bool = True
    # False: the profile's own system prompt lists its skills and how to read them; the
    # chat skill section (which points to view_text_file) is left out.
    native_skill_prompt: bool = True


_enabled: set[str] = set()
_agent_profiles: dict[str, AgentProfile] = {}
_sandbox_routes: list[tuple[str, Callable[[], Any]]] = []
_channel_routes: list[tuple[Callable[[Any, Any], bool], Callable[..., Awaitable[None]]]] = []
_lifecycle: list[tuple[Callable[[], Awaitable[None]], Callable[[], Awaitable[None]]]] = []
_routers: list[tuple[Any, Optional[str]]] = []


def mark_enabled(name: str) -> None:
    _enabled.add(name)


def is_enabled(name: str) -> bool:
    return name in _enabled


def register_agent_profile(name: str, profile: AgentProfile) -> None:
    _agent_profiles[name] = profile


def agent_profile(name: str) -> AgentProfile:
    profile = _agent_profiles.get(name)
    if profile is None:
        raise ValueError(f"Agent profile {name!r} is not enabled")
    return profile


def register_sandbox_route(session_prefix: str, provider: Callable[[], Any]) -> None:
    """Sandbox sessions whose id starts with ``session_prefix`` use ``provider()``."""
    _sandbox_routes.append((session_prefix, provider))


def sandbox_route(session_id: str) -> Optional[Callable[[], Any]]:
    for prefix, provider in _sandbox_routes:
        if session_id.startswith(prefix):
            return provider
    return None


def register_channel_route(
    handles: Callable[[Any, Any], bool],
    admit: Callable[..., Awaitable[None]],
) -> None:
    """``handles(db, conn)`` claims an inbound bot message; ``admit(conn, msg, adapter)`` takes it."""
    _channel_routes.append((handles, admit))


def channel_routes() -> tuple:
    return tuple(_channel_routes)


def register_lifecycle(
    start: Callable[[], Awaitable[None]],
    stop: Callable[[], Awaitable[None]],
) -> None:
    """Background work started on each service worker and stopped on shutdown."""
    _lifecycle.append((start, stop))


def lifecycle_hooks() -> tuple:
    return tuple(_lifecycle)


def register_router(router: Any, policy_key: Optional[str] = None) -> None:
    """An API router mounted with the same edition policy as ``EE_ROUTERS`` entries."""
    _routers.append((router, policy_key))


def routers() -> tuple:
    return tuple(_routers)
