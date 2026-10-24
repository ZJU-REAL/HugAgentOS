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
    # Framework-neutral per-agent policies and optional steering source.
    hooks: Callable[[Any], list] = field(default=lambda request: [])
    steering: Callable[[Any], Any] = field(default=lambda request: None)
    # Only function tools whose name passes are exposed; None keeps all.
    tool_filter: Optional[Callable[[str], bool]] = None
    # False: MCP servers come only from the request's explicit list, never catalog defaults.
    # False also prevents history and skill dependencies from widening explicit bindings.
    inherit_default_mcps: bool = True
    # False: the profile's own system prompt lists its skills and how to read them; the
    # chat skill section (which points to view_text_file) is left out.
    native_skill_prompt: bool = True


def mark_enabled(name: str) -> None:
    registration_state().enabled.add(name)


def is_enabled(name: str) -> bool:
    return name in registration_state().enabled


def register_agent_profile(name: str, profile: AgentProfile) -> None:
    registration_state().agent_profiles[name] = profile


def agent_profile(name: str) -> AgentProfile:
    profile = registration_state().agent_profiles.get(name)
    if profile is None:
        raise ValueError(f"Agent profile {name!r} is not enabled")
    return profile


def register_sandbox_route(session_prefix: str, provider: Callable[[], Any]) -> None:
    """Sandbox sessions whose id starts with ``session_prefix`` use ``provider()``."""
    registration_state().sandbox_routes.append((session_prefix, provider))


def sandbox_route(session_id: str) -> Optional[Callable[[], Any]]:
    for prefix, provider in registration_state().sandbox_routes:
        if session_id.startswith(prefix):
            return provider
    return None


def register_channel_route(
    handles: Callable[[Any, Any], bool],
    admit: Callable[..., Awaitable[None]],
) -> None:
    """``handles(db, conn)`` claims an inbound bot message; ``admit(conn, msg, adapter)`` takes it."""
    registration_state().channel_routes.append((handles, admit))


def channel_routes() -> tuple:
    return tuple(registration_state().channel_routes)


def register_lifecycle(
    start: Callable[[], Awaitable[None]],
    stop: Callable[[], Awaitable[None]],
) -> None:
    """Background work started on each service worker and stopped on shutdown."""
    registration_state().lifecycle.append((start, stop))


def lifecycle_hooks() -> tuple:
    return tuple(registration_state().lifecycle)


def register_router(router: Any, policy_key: Optional[str] = None) -> None:
    """An API router mounted with the same edition policy as ``EE_ROUTERS`` entries."""
    registration_state().routers.append((router, policy_key))


def routers() -> tuple:
    return tuple(registration_state().routers)


@dataclass(frozen=True)
class SandboxCredentials:
    """Contribution to cloud credentials; callbacks retain provider-specific SDK context."""
    name: str
    environment: Optional[Callable[[], dict[str, str]]] = None
    mounts: Optional[Callable[[Optional[str]], list[Any]]] = None
    before_command: Optional[Callable[[Any, Any, str, str], Awaitable[None]]] = None
    after_command: Optional[Callable[[Any, Any, str, str], Awaitable[None]]] = None
    prepare_order: int = 0


def register_sandbox_credentials(provider: SandboxCredentials) -> None:
    registration_state().sandbox_credentials[provider.name] = provider


def sandbox_credentials() -> tuple[SandboxCredentials, ...]:
    return tuple(registration_state().sandbox_credentials.values())


@dataclass(frozen=True)
class NativeToolContext:
    request: Any
    interactive: bool
    is_channel_run: bool
    sandbox_session_id: Any
    project_scope: Any
    skill_ids: Any
    enabled_mcp_keys: Any
    logger: Any


@dataclass(frozen=True)
class NativeToolContribution:
    name: str
    factory: Callable[[Any, NativeToolContext], Awaitable[None]]
    enabled: Callable[[NativeToolContext], bool]
    stage: str


def register_tool(name, factory, enabled, *, stage):
    """Register a conditional tool at an ordered native assembly stage."""
    registration_state().tools[name] = NativeToolContribution(name, factory, enabled, stage)


async def install_tools(stage, toolkit, context):
    for contribution in tuple(registration_state().tools.values()):
        if contribution.stage == stage and contribution.enabled(context):
            await contribution.factory(toolkit, context)


@dataclass
class RegistrationState:
    enabled: set[str] = field(default_factory=set)
    agent_profiles: dict[str, AgentProfile] = field(default_factory=dict)
    sandbox_routes: list[tuple[str, Callable[[], Any]]] = field(default_factory=list)
    channel_routes: list[tuple[Callable[[Any, Any], bool], Callable[..., Awaitable[None]]]] = field(default_factory=list)
    lifecycle: list[tuple[Callable[[], Awaitable[None]], Callable[[], Awaitable[None]]]] = field(default_factory=list)
    routers: list[tuple[Any, Optional[str]]] = field(default_factory=list)
    sandbox_credentials: dict[str, SandboxCredentials] = field(default_factory=dict)
    tools: dict[str, NativeToolContribution] = field(default_factory=dict)


_REGISTRATIONS = RegistrationState()


def registration_state() -> RegistrationState:
    return _REGISTRATIONS
