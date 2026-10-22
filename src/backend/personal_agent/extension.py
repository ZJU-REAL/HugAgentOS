"""Personal Agent module registration (see ``core.harness.registry``).

Everything Personal Agent adds to the running system is registered here. With
``PERSONAL_AGENT_ENABLED`` off nothing is registered: no routes, no background
worker, no channel routing, no dedicated agent profile and no prompt kind.
"""

import asyncio
import os

from core.harness.registry import AgentProfile, register_agent_profile, register_channel_route, register_lifecycle, register_router, register_sandbox_route

from personal_agent.access import NAME

def env_enabled() -> bool:
    return os.getenv("PERSONAL_AGENT_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")

async def _system_prompt(request) -> str:
    # Fixed platform rules plus the agent's workspace persona files (SOUL.md, USER.md, ...).
    from personal_agent.workspace.prompting import task_system_prompt

    return await asyncio.to_thread(task_system_prompt, request.profile_ref, list(request.enabled_skill_ids or []))

async def _front_prompt(request) -> str:
    # The agent's own turn: conversation rules and persona files. The per-turn context follows the message.
    from personal_agent.conversation.front import system_prompt

    return await asyncio.to_thread(system_prompt, request.profile_ref)

def _register_front_tools(toolkit, request, loader=None) -> None:
    from personal_agent.tools.front_tools import register_front_tools

    register_front_tools(toolkit, request, loader=loader)

def _register_tools(toolkit, request, loader=None) -> None:
    from personal_agent.tools.tools import register_personal_tools

    register_personal_tools(toolkit, request, loader=loader)

def _hooks(request) -> list:
    from personal_agent.computer.screenshot_pruning import screenshot_pruning_hook
    return [screenshot_pruning_hook()]


def _steering(request):
    from personal_agent.tasks.steering import current_steering
    steering = current_steering.get()
    return steering.hook_policy() if steering else None

def _handles_channel(db, conn) -> bool:
    from personal_agent.channels.channel_bridge import handled_by_agent

    return handled_by_agent(db, conn)

async def _admit_channel(conn, msg, adapter) -> None:
    from personal_agent.channels.channel_bridge import admit_inbound

    await admit_inbound(conn, msg, adapter)

def _sandbox_provider():
    from personal_agent.computer.sandbox import get_personal_sandbox_provider

    return get_personal_sandbox_provider()

async def _start() -> None:
    from personal_agent.tasks.worker import start_worker

    await start_worker()

async def _stop() -> None:
    from personal_agent.tasks.worker import stop_worker

    await stop_worker()

def setup() -> bool:
    if not env_enabled():
        return False
    from core.llm.prompt_kinds import register_kind
    from personal_agent.api.agent import router as agent_router
    from personal_agent.api.sessions import router as sessions_router

    from personal_agent.conversation.front import PROFILE as FRONT_PROFILE
    from personal_agent.workspace.constants import PROMPT_KIND
    from personal_agent.workspace.prompting import prompt_spec

    register_agent_profile(
        NAME,
        AgentProfile(
            system_prompt=_system_prompt,
            register_tools=_register_tools,
            hooks=_hooks,
            steering=_steering,
            tool_filter=lambda name: name.startswith("pa_"),
            inherit_default_mcps=False,
            # Skills are listed in the platform rules and read with pa_skill_read.
            native_skill_prompt=False,
        ),
    )
    # The agent's turn in the main conversation: it answers or delegates to tasks.
    register_agent_profile(
        FRONT_PROFILE,
        AgentProfile(
            system_prompt=_front_prompt,
            register_tools=_register_front_tools,
            tool_filter=lambda name: name.startswith("pa_"),
            inherit_default_mcps=False,
        ),
    )
    # Platform rules and workspace templates are edited in Config -> prompt management.
    register_kind(PROMPT_KIND, prompt_spec())
    register_sandbox_route("pa_box_", _sandbox_provider)
    register_channel_route(_handles_channel, _admit_channel)
    register_lifecycle(_start, _stop)
    register_router(sessions_router)
    register_router(agent_router)
    return True
