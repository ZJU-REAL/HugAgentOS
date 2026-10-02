"""Agent assembly phase: profile selection. """

from __future__ import annotations

import logging

from core.llm.factory.models import RequiredCapabilities, StickyCapabilities

logger = logging.getLogger(__name__)
import asyncio

import core.config.catalog as catalog
from core.auth.tenancy import tenant_of
from core.evolution.agent_profile import builtin_profile, load_active_profile
from core.llm.execution_manifest import PromptManifestBuilder
from core.llm.factory.request import AgentRequest


async def profile_selection(
    request: AgentRequest,
    *,
    _log,
    required: RequiredCapabilities,
    sticky: StickyCapabilities,
    skill_ids_for_bindings,
):
    profile = builtin_profile()
    if request.user_agent is None:
        # A user-built sub-agent carries its own explicit bindings. Applying a
        # learned profile on top would override a person's deliberate
        # configuration with a statistical one, which is not a trade the person
        # agreed to.
        try:
            profile = await asyncio.to_thread(
                load_active_profile,
                task_type=str(request.chat_mode or "chat"),
                user_id=request.current_user_id or "",
                tenant_id=tenant_of(request.current_user_id),
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("[factory] profile load failed, using built-in: %s", exc)

    if profile.skill_ids:
        # The profile decides what this task type gets offered at all. Anything
        # outside it is not a candidate, which is the mechanism that stops a
        # skill distilled from one family from being in view during another —
        # the leak that text annotations were measured to be unable to close.
        allowed = set(profile.skill_ids)
        skill_ids_for_bindings = [
            sid for sid in (skill_ids_for_bindings or []) if sid in allowed
        ] or list(profile.skill_ids)
        if request.enabled_skill_ids is not None:
            request.enabled_skill_ids = [sid for sid in request.enabled_skill_ids if sid in allowed]
        # A learned/default orchestration profile may narrow ambient skills,
        # but it must not erase chat-sticky or currently explicit capabilities.
        skill_ids_for_bindings = list(
            dict.fromkeys(
                [
                    *skill_ids_for_bindings,
                    *sticky.plugin_skill_ids,
                    *sticky.direct_skill_ids,
                    *required.plugin_skill_ids,
                    *([required.skill_id] if required.skill_id else []),
                ]
            )
        )
        if request.enabled_skill_ids is not None:
            request.enabled_skill_ids = list(
                dict.fromkeys(
                    [
                        *request.enabled_skill_ids,
                        *sticky.plugin_skill_ids,
                        *sticky.direct_skill_ids,
                        *required.plugin_skill_ids,
                        *([required.skill_id] if required.skill_id else []),
                    ]
                )
            )

    # ── Skill availability evidence (GCE ticket 10) ─────────────────────────
    # Which skills were available to the model this turn. Skill *loading* keeps
    # its own logic — every enabled skill's name and description goes into the
    # prompt and the model opens what it wants — so this records availability,
    # not a ranked choice, and ``degraded`` says so.
    #
    # Whether a skill was actually *used* is a separate observation
    # (``skill.opened``), recorded from the tool log. Keeping the two apart is
    # what the decremental engine needs; narrowing what gets loaded is not.
    skill_selection = None
    try:
        from core.agent_skills.selection_record import build_selection

        skill_selection = build_selection(
            all_candidate_ids=list(skill_ids_for_bindings or []),
            selected_ids=list(skill_ids_for_bindings or []),
            strategy="passthrough",
            degraded=True,
            degrade_reason="top_k_disabled",
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("[skill-select] record skipped: %s", exc)

    # The profile's sub-agent routes, applied. A route says "for this task type,
    # this work goes to a separate context"; anything the profile does not route
    # to stays out of view for this task type, which is the same narrowing
    # argument as the tool allowlist — an unroutable sub-agent in the prompt is
    # a delegation the model can attempt and should not.
    #
    # Evolution-authored agents ship disabled until their route is approved as a
    # prompt change, so this never surfaces one the reviewer has not seen.
    if profile.subagent_routes and request.visible_subagents:
        routed = {
            route.agent_id
            for route in profile.subagent_routes
            if not route.task_types or str(request.chat_mode or "chat") in route.task_types
        }
        request.visible_subagents = [
            agent
            for agent in request.visible_subagents
            if str(agent.get("agent_id") or "") in routed
        ]

    # The profile's tool allowlist, applied. It can only narrow: the candidate
    # that produced it was validated against the base grant, and intersecting
    # here means even a stored profile that has outlived a narrowed grant cannot
    # re-widen it.
    # Turbo's retrieval trio is a product contract, not a learned assembly —
    # a stored profile allowlist must not re-narrow it (it would silently drop
    # KB / web_fetch and leave quick lookup search-only).
    if profile.tool_allowlist is not None and not request.turbo_mode:
        allowed_tools = set(profile.tool_allowlist)
        current_mcp = (
            request.enabled_mcp_ids
            if isinstance(request.enabled_mcp_ids, list)
            else [item for item in catalog.get_enabled_ids("mcp") if isinstance(item, str)]
        )
        request.enabled_mcp_ids = [mcp_id for mcp_id in current_mcp if mcp_id in allowed_tools]
        # A user-selected connector must not disappear silently behind a learned
        # profile. It remains subject to the real server/ownership gate below.
        request.enabled_mcp_ids = list(
            dict.fromkeys(
                [
                    *request.enabled_mcp_ids,
                    *sticky.plugin_mcp_ids,
                    *sticky.direct_mcp_ids,
                    *required.connector_ids,
                    *required.plugin_mcp_ids,
                ]
            )
        )
        _log.info(
            "[factory] profile %s narrowed MCP servers %d → %d",
            profile.profile_id,
            len(current_mcp),
            len(request.enabled_mcp_ids),
        )

    # The execution manifest is assembled alongside the real prompt/tool
    # request. It hashes complete inputs but only persists hashes and public
    # references. Binding happens after the final prompt and tool schemas exist,
    # still before the Agent can execute anything.
    _manifest_builder = PromptManifestBuilder(
        context={
            "workspace_id": str(request.workspace_id or "default"),
            "project_id": str((request.project_ctx or {}).get("project_id") or ""),
            "project": dict(request.project_ctx or {}),
            "chat_mode": str(request.chat_mode or "default"),
            "model": {
                "name": str(request.model_name or ""),
                "provider_id": str(request.model_provider_id or ""),
            },
            "capabilities": {
                "skill_ids": list(skill_ids_for_bindings or []),
                "mcp_ids": list(request.enabled_mcp_ids or []),
                "kb_ids": list(request.enabled_kb_ids or []),
            },
            "orchestration_profile_id": str(profile.profile_id),
            "workflow_policy_version": str(profile.version),
            "prompt_fragment_ids": list(profile.prompt_fragments or []),
        }
    )

    return (
        _manifest_builder,
        profile,
        skill_ids_for_bindings,
        skill_selection,
    )
