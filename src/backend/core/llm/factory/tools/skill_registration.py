"""Agent assembly phase: skill registration. """

from __future__ import annotations

import asyncio
import os

from core.capabilities import runtime as capability_runtime
from core.capabilities.paths import capabilities_enabled
from core.llm.factory.request import AgentRequest
from core.llm.factory.selection import capabilities as factory_capabilities


async def skill_registration(
    request: AgentRequest, *, _api_scope, _eval_scope, _log, _prepared_capabilities, loader, toolkit
):
    skill_ids_to_register = request.enabled_skill_ids
    if skill_ids_to_register is None:
        skill_ids_to_register = factory_capabilities._effective_main_available_skills()
        # The fallback resolves from the static catalog, which today carries no
        # evolution-authored ids — but the exposure gate is applied here anyway
        # rather than relying on that. A gate that only covers the paths we
        # happened to think of is not a gate.
        skill_ids_to_register = factory_capabilities._filter_skill_ids_for_user(
            skill_ids_to_register, request.current_user_id
        )
    # Note: a subagent's (user_agent) enabled_skill_ids is always a list ([]
    # when unconfigured) and never hits the None fallback above — i.e. "a
    # subagent with no skills configured has no skills"; strictly per its own
    # config, no inheriting the full catalog set.

    if _prepared_capabilities is not None:
        missing = set(skill_ids_to_register or []) - set(_prepared_capabilities.bindings)
        if missing:
            from core.capabilities.errors import PackageMissing

            raise PackageMissing(
                "selected skills are absent from this prepared run",
                details={"skills": sorted(missing)},
            )
        loader = await asyncio.to_thread(capability_runtime.frozen_loader, _prepared_capabilities)

    allowed_skill_dirs: list[str] = []
    if not request.disable_tools and skill_ids_to_register:
        n = loader.register_skills_to_toolkit(toolkit, skill_ids_to_register)
        if n > 0:
            _log.info("Registered %d agent skills to toolkit", n)
        for sid in skill_ids_to_register:
            d = loader.get_skill_dir(sid)
            if d:
                allowed_skill_dirs.append(d)

    if (
        not request.disable_tools
        and not capabilities_enabled()
        and _api_scope is None
        and _eval_scope is None
    ):
        from core.agent_skills.config import (
            get_enabled_skill_sources,
            get_sandbox_skills_dir,
            get_user_skills_dir,
        )

        for src in get_enabled_skill_sources():
            root = str(src.root_dir)
            if os.path.isdir(root) and root not in allowed_skill_dirs:
                allowed_skill_dirs.append(root)
        # Shared skills dir + this user's own dir (see the layout note in
        # agent_skills.config). Blanket-allow both so view_text_file can read any
        # materialized skill the user is entitled to, even one not in
        # skill_ids_to_register — and only those: another user's private skill
        # files stay outside the allow-list.
        _own_roots = [get_sandbox_skills_dir(), get_user_skills_dir(request.current_user_id)]
        for _root in _own_roots:
            if _root is None:
                continue
            if str(_root) not in allowed_skill_dirs:
                allowed_skill_dirs.append(str(_root))

    if _prepared_capabilities is not None:
        # File tools may read exactly this run's revisions, never the entire
        # capability root (which contains other accounts and business data).
        allowed_skill_dirs = [
            os.path.realpath(loader.get_skill_dir(name)) for name in _prepared_capabilities.bindings
        ]

    return (
        allowed_skill_dirs,
        loader,
        skill_ids_to_register,
    )
