"""Agent assembly phase: system prompt. """

from __future__ import annotations

import os
from typing import Any, Dict

from core.llm.factory.defaults import _BATCH_MODE_HINT, _WORKFLOW_MODE_HINT
from core.llm.factory.request import AgentRequest
from core.ontology.validator import render_runtime_prompt
from core.services.system_config import code_capability_enabled
from prompts.prompt_runtime import build_subagent_system_prompt, build_system_prompt


async def system_prompt(
    request: AgentRequest,
    *,
    _agent_ref,
    _elapsed,
    _log,
    _manifest_builder,
    _ontology_hidden_tools,
    _ontology_runtime,
    _plan_tool_enabled,
    _plugin_runtime,
    _progressive,
    _sbx_sess,
    _subagent_progressive,
    _tool_approval_available,
    _turbo_code_exec,
    cfg,
    enabled_mcp_keys,
    loader,
    skill_ids_to_register,
    tool_schemas,
    toolkit,
):
    if request.user_agent is not None:
        system_prompt = build_subagent_system_prompt(
            request.user_agent,
            tool_schemas,
            enabled_mcp_keys,
            enabled_kb_ids=request.enabled_kb_ids,
        )
        from core.config.local_mode import local_mode_enabled

        if local_mode_enabled():
            from prompts.desktop_workspace import desktop_prompt_text

            system_prompt = desktop_prompt_text(system_prompt)
        _manifest_builder.add_prompt_section(
            "subagent/base",
            system_prompt,
            origin=f"user-agent:{getattr(request.user_agent, 'agent_id', '') or 'configured'}",
            trust="user_configured",
            priority=10,
            cache_class="agent",
            version=str(getattr(request.user_agent, "updated_at", "") or "1"),
            reference=f"agent:{getattr(request.user_agent, 'agent_id', '') or 'configured'}",
            sensitive=True,
        )
        _log.info(
            "[factory] +%s subagent system prompt built (%d chars)",
            _elapsed(),
            len(system_prompt),
        )
        _ontology_prompt = render_runtime_prompt(_ontology_runtime)
        if _ontology_prompt:
            system_prompt += "\n\n" + _ontology_prompt
            _manifest_builder.add_prompt_section(
                "runtime/ontology",
                _ontology_prompt,
                origin="ontology:runtime",
                trust="governed_runtime",
                priority=850,
                cache_class="run",
                version=str(_ontology_runtime.get("revision") or "1"),
                sensitive=True,
            )
            _log.info(
                "[factory] +%s subagent ontology contract injected (%d chars)",
                _elapsed(),
                len(_ontology_prompt),
            )

        # ── Progressive plugin directory + load_plugin tool (sub-agent) ──
        # Bound plugins deferred in the override block above; in-run activation
        # only (persist=False — see the note there).
        if _subagent_progressive is not None:
            from core.plugins import runtime as _plug

            _plugin_dir_section = _plug.build_plugin_directory_section(
                _subagent_progressive.directory
            )
            if _plugin_dir_section:
                system_prompt += "\n\n" + _plugin_dir_section
                _manifest_builder.add_prompt_section(
                    "runtime/plugin_directory",
                    _plugin_dir_section,
                    origin="plugin:directory",
                    trust="configured_service",
                    priority=820,
                    cache_class="capability_set",
                    version="1",
                    sensitive=True,
                )
            _plugin_runtime = {
                "activated_slugs": set(),
                "connected_keys": set(enabled_mcp_keys),
                "toolkit": None,
                "permission_context": None,
                "close_list": None,
                "persist": False,
                "loader": loader,
                "chat_id": request.chat_id,
                "user_id": request.current_user_id,
                "enabled_kb_ids": request.enabled_kb_ids,
                "channel_origin": request.channel_origin,
                "reranker_enabled": request.reranker_enabled,
                "approval_available": _tool_approval_available,
                "ontology_runtime": _ontology_runtime,
            }
            _plug.register_load_plugin(
                toolkit, _subagent_progressive.deferred_by_slug(), _plugin_runtime
            )
            _log.info(
                "[factory] +%s subagent progressive plugins: %d deferred (skills=%d, mcp=%d)",
                _elapsed(),
                len(_subagent_progressive.deferred),
                len(_subagent_progressive.deferred_skill_ids),
                len(_subagent_progressive.deferred_mcp_ids),
            )
    else:
        # ── 模式自带的专属提示词 ──
        # 手写正文优先于绑定的版本池分类；两者都空则该模式没配提示词。
        # 这段刻意放在 turbo 分支**之外**：收窄与否（tool_scope）和"要不要换提示词"
        # 是两件正交的事——一个不收窄工具面的模式（比如给标准模式换个口吻）同样该
        # 能配自己的提示词。之前把它写在 turbo 分支里，非收窄模式配了也不生效。
        _mode_prompt = ""
        if request.mode_spec is not None:
            from core.services import prompt_version_service as _pvs_mode

            _mode_prompt_text = getattr(request.mode_spec, "prompt_text", None)
            _mode_prompt_kind = getattr(request.mode_spec, "prompt_kind", None)
            if _mode_prompt_text:
                _mode_prompt = str(_mode_prompt_text)
            elif _mode_prompt_kind and _mode_prompt_kind != "turbo":
                _mode_prompt = _pvs_mode.render_kind_segment(_mode_prompt_kind, fs_fallback=False)

        if request.turbo_mode and not _turbo_code_exec:
            # Turbo swaps in the standalone prompt (DB "turbo" active version →
            # fs fallback): the default prompt's tool/workflow sections describe
            # capabilities this assembly deliberately does not carry.
            # （开了代码执行位的收窄模式不进这条：装配确实带沙箱/文件工具，
            # turbo 正文"秒级检索、不执行代码"的叙事反而是错的——没配专属
            # 提示词时走默认装配，工具段按实际 toolkit 动态生成。）
            from core.services import prompt_version_service as _pvs_turbo

            # 收窄模式没配专属提示词时退回历史的 turbo 正文——极速模式绑的就是它。
            system_prompt = _mode_prompt or _pvs_turbo.render_turbo_system_prompt()
            from core.config.local_mode import local_mode_enabled

            if local_mode_enabled():
                from prompts.desktop_workspace import desktop_prompt_text

                system_prompt = desktop_prompt_text(system_prompt)
            _manifest_builder.add_prompt_section(
                "mode/base",
                system_prompt,
                origin=f"chat-mode:{getattr(request.mode_spec, 'slug', None) or 'turbo'}",
                trust="admin",
                priority=10,
                cache_class="mode",
                version=str(getattr(request.mode_spec, "updated_at", "") or "1"),
            )
            _log.info(
                "[factory] +%s turbo system prompt built (%d chars)",
                _elapsed(),
                len(system_prompt),
            )
        elif _mode_prompt:
            # 不收窄的模式配了专属提示词：整段替换默认装配（和收窄模式同一语义），
            # 但工具/技能面不动——那是 tool_scope 管的事。
            system_prompt = _mode_prompt
            from core.config.local_mode import local_mode_enabled

            if local_mode_enabled():
                from prompts.desktop_workspace import desktop_prompt_text

                system_prompt = desktop_prompt_text(system_prompt)
            _manifest_builder.add_prompt_section(
                "mode/base",
                system_prompt,
                origin=f"chat-mode:{getattr(request.mode_spec, 'slug', 'configured')}",
                trust="admin",
                priority=10,
                cache_class="mode",
                version=str(getattr(request.mode_spec, "updated_at", "") or "1"),
            )
            _log.info(
                "[factory] +%s mode system prompt built (%d chars, slug=%s)",
                _elapsed(),
                len(system_prompt),
                getattr(request.mode_spec, "slug", "?"),
            )
        else:
            _sp_ctx: Dict[str, Any] = {
                "tools": tool_schemas,
                "mcp_servers": enabled_mcp_keys,
                "enabled_kbs": request.enabled_kb_ids,
                "chat_id": request.chat_id,
                "sandbox_session_id": _sbx_sess,
                "approval_mode": request.approval_mode,
            }
            # Project mode: let _build_project_section receive project_name / instructions / files / folder
            if request.project_ctx:
                _sp_ctx.update(request.project_ctx)
            system_prompt = build_system_prompt(
                cfg, ctx=_sp_ctx, manifest_builder=_manifest_builder
            )
            # Project material must travel as its own canonical ContextItem so
            # it can be independently budgeted and audited.  The prompt
            # builder retains this plaintext only in memory; persisted
            # execution manifests still contain hashes/references alone.
            for (
                _section,
                _section_content,
            ) in _manifest_builder.prompt_section_sources():
                if _section.id != "runtime/project" or not _section_content:
                    continue
                _start = system_prompt.rfind(_section_content)
                if _start < 0:
                    continue
                _before = system_prompt[:_start].rstrip()
                _after = system_prompt[_start + len(_section_content) :].lstrip()
                system_prompt = (
                    _before + ("\n\n" + _after if _before and _after else _after)
                ).strip()
            _log.info(
                "[factory] +%s system prompt built (%d chars)",
                _elapsed(),
                len(system_prompt),
            )

        _ontology_prompt = render_runtime_prompt(_ontology_runtime)
        if _ontology_prompt:
            system_prompt += "\n\n" + _ontology_prompt
            _manifest_builder.add_prompt_section(
                "runtime/ontology",
                _ontology_prompt,
                origin="ontology:runtime",
                trust="governed_runtime",
                priority=850,
                cache_class="run",
                version=str(_ontology_runtime.get("revision") or "1"),
                sensitive=True,
            )
            _log.info(
                "[factory] +%s ontology contract injected (%d chars, hidden_tools=%d)",
                _elapsed(),
                len(_ontology_prompt),
                len(_ontology_hidden_tools),
            )

        # ── Inject code-capability system prompt ──
        # Gating: CODE_CAPABILITY_ENABLED=true injects in all modes.
        # Single source of truth render_kind_segment (same source as the Config console preview).
        if code_capability_enabled() and (not request.turbo_mode or _turbo_code_exec):
            try:
                from core.services import prompt_version_service as _pvs

                _code_exec_text = _pvs.render_kind_segment("code_exec")
            except Exception:
                _code_exec_text = ""
            if _code_exec_text:
                system_prompt += "\n\n" + _code_exec_text
                _manifest_builder.add_prompt_section(
                    "runtime/code_capability",
                    _code_exec_text,
                    origin="prompt-version:code_exec",
                    trust="admin",
                    priority=750,
                    cache_class="capability_set",
                    version="1",
                )
                _log.info(
                    "[factory] +%s code execution prompt injected (%d chars)",
                    _elapsed(),
                    len(_code_exec_text),
                )

        # ── Progressive plugin directory + load_plugin tool ──
        # The directory section is byte-stable per user (sorted by slug,
        # independent of activation state) so an activation never perturbs this
        # part of the prefix; only the tools/skills it adds do, once.
        if _progressive is not None:
            from core.plugins import runtime as _plug

            _plugin_dir_section = _plug.build_plugin_directory_section(_progressive.directory)
            if _plugin_dir_section:
                system_prompt += "\n\n" + _plugin_dir_section
                _manifest_builder.add_prompt_section(
                    "runtime/plugin_directory",
                    _plugin_dir_section,
                    origin="plugin:directory",
                    trust="configured_service",
                    priority=820,
                    cache_class="capability_set",
                    version="1",
                    sensitive=True,
                )
            _plugin_runtime = {
                "activated_slugs": set(_progressive.activated_slugs),
                "connected_keys": set(enabled_mcp_keys),
                "toolkit": None,  # the real Toolkit, filled after construction
                "permission_context": None,  # filled after AgentRuntimeState exists
                "close_list": None,  # filled right before return
                "loader": loader,
                "chat_id": request.chat_id,
                "user_id": request.current_user_id,
                "enabled_kb_ids": request.enabled_kb_ids,
                "channel_origin": request.channel_origin,
                "reranker_enabled": request.reranker_enabled,
                "approval_available": _tool_approval_available,
                "ontology_runtime": _ontology_runtime,
            }
            _plug.register_load_plugin(toolkit, _progressive.deferred_by_slug(), _plugin_runtime)
            _log.info(
                "[factory] +%s progressive plugins: %d in directory, %d deferred "
                "(skills=%d, mcp=%d)",
                _elapsed(),
                len(_progressive.directory),
                len(_progressive.deferred),
                len(_progressive.deferred_skill_ids),
                len(_progressive.deferred_mcp_ids),
            )

        # ── Inject batch execution hint (App Center batch-execution sessions only) ──
        if request.batch_mode:
            system_prompt += _BATCH_MODE_HINT
            _manifest_builder.add_prompt_section(
                "runtime/batch_mode",
                _BATCH_MODE_HINT,
                origin="builtin:batch_mode",
                trust="platform",
                priority=880,
                cache_class="mode",
                version="1",
            )
            _log.info("[factory] +%s batch mode hint injected", _elapsed())

        # ── Inject workflow-mode hint (user explicitly entered workflow mode) ──
        if request.workflow_mode:
            system_prompt += _WORKFLOW_MODE_HINT
            _manifest_builder.add_prompt_section(
                "runtime/workflow_mode",
                _WORKFLOW_MODE_HINT,
                origin="builtin:workflow_mode",
                trust="platform",
                priority=880,
                cache_class="mode",
                version="1",
            )
            _log.info("[factory] +%s workflow mode hint injected", _elapsed())

        # ── Register call_subagent tool for main agent ──
        if request.visible_subagents:
            from core.llm.builtin_subagents import refresh_builtin_subagents
            from core.llm.subagent_tool import build_subagent_prompt_section, register_subagent_tool

            # Refresh platform-default rows only after the parent toolset has
            # completed catalog defaults, permission filtering, skill-bound MCP
            # expansion, and runtime feature gates. The same snapshot drives the
            # routing prompt and the eventual child executor, so disabled tools
            # are neither advertised nor delegated.
            _subagent_parent_runtime = {
                "enabled_skill_ids": list(skill_ids_to_register or []),
                "enabled_mcp_ids": list(enabled_mcp_keys),
                "enabled_kb_ids": list(request.enabled_kb_ids or []),
                "sandbox_tools_enabled": (
                    os.getenv("SANDBOX_TOOLS_ENABLED", "true").lower() == "true"
                ),
                "code_capability_enabled": bool(code_capability_enabled()),
                "reranker_enabled": request.reranker_enabled,
                "model_name": request.model_name,
                "model_provider_id": request.model_provider_id,
                "chat_mode": request.chat_mode,
                "chat_id": request.chat_id,
                "sandbox_session_id": _sbx_sess,
                "project_ctx": request.project_ctx,
                "channel_origin": request.channel_origin,
                "automation_run": request.automation_run,
                "run_id": request.run_id,
                "journal_owner": request.journal_owner,
                "capability_scope": request.capability_scope,
            }
            request.visible_subagents = refresh_builtin_subagents(
                request.visible_subagents,
                _subagent_parent_runtime,
            )
            _agent_ref = {"agent": None}  # set after creation
            register_subagent_tool(
                toolkit,
                request.visible_subagents,
                request.current_user_id or "",
                agent_ref=_agent_ref,
                chat_id=request.chat_id,
                parent_runtime=_subagent_parent_runtime,
            )
            # `mentioned_agent_ids` is consumed by the caller via
            # build_subagent_mention_hint() and injected into the current user
            # message — NOT into the system prompt. Keeping it out of the
            # system prompt preserves the LLM provider's prefix cache.
            subagent_section = build_subagent_prompt_section(request.visible_subagents)
            if subagent_section:
                system_prompt = system_prompt + "\n\n" + subagent_section
                _manifest_builder.add_prompt_section(
                    "runtime/subagent_directory",
                    subagent_section,
                    origin="subagent:directory",
                    trust="configured_service",
                    priority=830,
                    cache_class="capability_set",
                    version="1",
                    sensitive=True,
                )
            _log.info(
                "[factory] +%s subagent tool registered (%d agents)",
                _elapsed(),
                len(request.visible_subagents),
            )

        # ── Register update_plan tool (top-level interactive main conversations only, positive opt-in) ──
        # Codex-style lightweight plan tracker: for complex tasks the main
        # agent maintains a step checklist and keeps executing in the same
        # turn (no redirect, no approval gate); the frontend renders it as a
        # plan bar above the chat input. This replaced the old
        # enter_plan_mode redirect for model-initiated planning.
        # Recognizes ONLY the single positive signal top_level_chat (passed in
        # by astream_chat_workflow after it determines this is an interactive
        # main conversation) — not a negative exclusion list of "not batch and
        # not plan_mode and not …". A negative list leaks the tool with every
        # derived context it misses (historically, plan-execute steps,
        # plan-generation disable_tools, and channel runs all leaked this way);
        # a positive opt-in has one single source of truth, and all
        # derived/non-interactive constructions get nothing by default. The DB
        # switch auto_plan_entry_enabled (which itself returns False on
        # config-layer errors) can turn this off entirely.
        from core.services.system_config import auto_plan_entry_enabled

        if request.top_level_chat and auto_plan_entry_enabled():
            from core.llm.plan_update_tool import (
                build_plan_update_prompt_section,
                register_plan_update_tool,
            )

            # 计划栏的催更中间件挂在同一个正向开关上：工具没注册就绝不该有人催更新。
            _plan_tool_enabled = True
            register_plan_update_tool(toolkit)
            _pu_section = build_plan_update_prompt_section()
            if _pu_section:
                system_prompt = system_prompt + "\n\n" + _pu_section
                _manifest_builder.add_prompt_section(
                    "runtime/plan_tool",
                    _pu_section,
                    origin="builtin:update_plan",
                    trust="platform",
                    priority=840,
                    cache_class="capability_set",
                    version="1",
                )
            _log.info(
                "[factory] +%s update_plan tool registered (chat_id=%s)",
                _elapsed(),
                request.chat_id,
            )

    return (
        _agent_ref,
        _plan_tool_enabled,
        _plugin_runtime,
        system_prompt,
    )
