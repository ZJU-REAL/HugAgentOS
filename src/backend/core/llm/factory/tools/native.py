"""Agent assembly phase: native tools. """

from __future__ import annotations

import asyncio

from core.llm.factory.request import AgentRequest
from core.llm.tools import (
    ReadStateTracker,
    register_bash,
    register_channel_attachment,
    register_edit,
    register_get_data_context,
    register_glob,
    register_grep,
    register_pin_to_workspace,
    register_read,
    register_read_artifact,
    register_read_image,
    register_sandbox_get_artifact,
    register_sandbox_put_artifact,
    register_sandboxed_view_text_file,
    register_space_tools,
    register_write,
)
from core.services.system_config import code_capability_enabled


async def native_tools(
    request: AgentRequest,
    *,
    _api_scope,
    _eval_scope,
    _interactive,
    _is_channel_run,
    _log,
    _proj_scope,
    _sbx_sess,
    _shared_subagent_cfg,
    _turbo_code_exec,
    allowed_skill_dirs,
    enabled_mcp_keys,
    loaded_skill_ids,
    loader,
    skill_ids_to_register,
    toolkit,
):
    if (
        not request.disable_tools
        and (not request.turbo_mode or _turbo_code_exec)
        and _api_scope is None
        and _eval_scope is None
    ):
        register_sandboxed_view_text_file(
            toolkit,
            allowed_skill_dirs,
            loader,
            loaded_skill_ids=loaded_skill_ids,
        )

        # ── Phase 3.5: Register sandbox tools (Bash + artifact in/out) ──
        # Skill files reach the sandbox via the unified /workspace/skills bind
        # mount (built-in synced at startup, DB skills materialized on demand —
        # see agent_skills.config.get_sandbox_skills_dir), so Bash needs no
        # per-call sync. loader/loaded_skill_ids kept for backward compat.
        if request.allow_bash:
            register_bash(
                toolkit,
                loader=loader,
                loaded_skill_ids=loaded_skill_ids,
                chat_id=request.chat_id,
                sandbox_session_id=_sbx_sess,
                user_id=request.current_user_id,
                scope=_proj_scope,
            )
        if not request.read_only:
            register_sandbox_put_artifact(
                toolkit,
                chat_id=request.chat_id,
                sandbox_session_id=_sbx_sess,
                user_id=request.current_user_id,
            )
        from core.config.local_mode import local_mode_enabled

        if not local_mode_enabled():
            register_sandbox_get_artifact(
                toolkit,
                chat_id=request.chat_id,
                sandbox_session_id=_sbx_sess,
                user_id=request.current_user_id,
                scope=_proj_scope,
            )
        # Site publishing is now plugin-based: the sites plugin's site_publish
        # MCP provides the publish_site tool; the built-in native tool is no
        # longer registered here (see mcp_servers/site_publish_mcp +
        # plugin_bundles/marketplace/sites).

        # Site-builder design pick (choose one of three): registered only for
        # sessions with the site-builder skill enabled (per-run conditional
        # registration, not in the catalog). Interactivity uses the second-tier
        # judgment _ui_reachable (≠ _interactive): write confirmation has the
        # allow_session out-of-band pre-authorization path on IM channels and
        # the automation confirmation panel in automation sessions, but the
        # suspended picker has no clickable UI in either place — so degrade to
        # non-interactive (the tool just lets the model pick its own design
        # instead of suspending for 2h).
        from core.llm.tools import design_picker_tool

        _ui_reachable = _interactive and not _is_channel_run and not request.automation_run
        if skill_ids_to_register and any(
            design_picker_tool.skill_uses_choose_design(str(sid)) for sid in skill_ids_to_register
        ):
            design_picker_tool.register_choose_design(
                toolkit,
                chat_id=request.chat_id,
                interactive=_ui_reachable,
                sandbox_session_id=_sbx_sess,
                user_id=request.current_user_id,
                scope=_proj_scope,
            )

        # ── Phase 3.6: Register file-operation tools (Read/Edit/Write/Glob/
        # Grep/Delete/Move + myspace). These tools share a single
        # ReadStateTracker, keeping the Edit/Write "must Read first" invariant
        # consistent across multiple tool calls.
        #
        # Gating (docs §3.2): CODE_CAPABILITY_ENABLED=true → available by
        # default in all modes. This block is already nested inside
        # `if not disable_tools:`, so the plan-generation phase naturally gets
        # no file capability.
        # Project mode: hooks up the folder name + subtree scoping; the fs/
        # MySpace tools below and pin_to_workspace (Phase 3.8) share the same
        # scope.
        _proj_folder_name = (request.project_ctx or {}).get("project_folder_name") or None

        # Decided once per build from the model this run will actually use: a
        # pinned subagent model wins over the request's provider (mirrors the
        # sub-agent override below and DynamicModelMiddleware).
        from core.vision import resolve_vision_mode

        _vision_mode = resolve_vision_mode(
            (_shared_subagent_cfg.provider_id if _shared_subagent_cfg else None)
            or getattr(request.user_agent, "model_provider_id", None)
            or request.model_provider_id
            or ""
        )

        if code_capability_enabled():
            _read_state = ReadStateTracker()
            register_read(
                toolkit,
                chat_id=request.chat_id,
                sandbox_session_id=_sbx_sess,
                user_id=request.current_user_id,
                state=_read_state,
                project_folder_name=_proj_folder_name,
                scope=_proj_scope,
                vision_mode=_vision_mode,
            )
            if not request.read_only:
                register_edit(
                    toolkit,
                    chat_id=request.chat_id,
                    sandbox_session_id=_sbx_sess,
                    user_id=request.current_user_id,
                    state=_read_state,
                    interactive=_interactive,
                    project_folder_name=_proj_folder_name,
                    scope=_proj_scope,
                )
                register_write(
                    toolkit,
                    chat_id=request.chat_id,
                    sandbox_session_id=_sbx_sess,
                    user_id=request.current_user_id,
                    state=_read_state,
                    interactive=_interactive,
                    project_folder_name=_proj_folder_name,
                    scope=_proj_scope,
                )
            register_glob(
                toolkit,
                chat_id=request.chat_id,
                sandbox_session_id=_sbx_sess,
                user_id=request.current_user_id,
                project_folder_name=_proj_folder_name,
                scope=_proj_scope,
            )
            register_grep(
                toolkit,
                chat_id=request.chat_id,
                sandbox_session_id=_sbx_sess,
                user_id=request.current_user_id,
                project_folder_name=_proj_folder_name,
                scope=_proj_scope,
            )
            if not request.read_only:
                register_space_tools(
                    toolkit,
                    chat_id=request.chat_id,
                    sandbox_session_id=_sbx_sess,
                    user_id=request.current_user_id,
                    state=_read_state,
                    interactive=_interactive,
                    project_folder_name=_proj_folder_name,
                    scope=_proj_scope,
                )

        # ── Phase 3.7: Register read_artifact for cross-turn file access ──
        # Unconditional: any user may have uploaded files in prior turns of this chat,
        # and the hook injects historical-file summaries referencing this tool.
        register_read_artifact(toolkit, user_id=request.current_user_id)

        # ── Phase 3.7a: read_image ──
        # Same rationale as read_artifact — images can arrive in any run (upload,
        # channel attachment, a chart the agent just rendered), and read_artifact
        # cannot parse them. Native mode hands over pixels, bridge mode a
        # transcription; in "none" mode the tool is not registered and Read says so.
        register_read_image(
            toolkit,
            chat_id=request.chat_id,
            sandbox_session_id=_sbx_sess,
            user_id=request.current_user_id,
            project_folder_name=_proj_folder_name,
            scope=_proj_scope,
            vision_mode=_vision_mode,
        )

        # ── Phase 3.7b: channel_read_attachment (channel runs only) ──
        # Group listening records bystander attachments by key without downloading them;
        # this is how the agent pulls one in on demand. Gated on channel runs so the tool
        # never clutters the toolkit of web conversations, where it could never resolve.
        if _is_channel_run:
            register_channel_attachment(
                toolkit, user_id=request.current_user_id, chat_id=request.chat_id
            )

        # ── Phase 3.8: Register pin_to_workspace ──
        # Lets the agent gate which generated files reach the user-visible
        # assistant message. See core/llm/workspace.py for the per-run state.
        register_pin_to_workspace(toolkit, scope=_proj_scope, sandbox_session_id=_sbx_sess)

        # ── Phase 3.85: run_job（工作流模式的作业编排面） ──
        # **用户显式触发才注册**（workflow_mode）：斜杠命令 /workflow 或 + 菜单选「工作流
        # 模式」。与计划模式/批量执行同属用户触发的模式——不触发就完全不存在，普通问答的
        # 工具面与提示词一点都不受影响。
        # 触发之后它才是这段对话的原生能力（不走 catalog 开关、关不掉）：面对 N 个同构
        # 工作项时，主循环逐项处理会让每一轮重发全部历史（成本随进度二次方增长，做不完
        # 就自行收工）。run_job 把循环体交给沙箱脚本，模型调用由后端代持凭据派出——
        # 脚本因此拿不到任何 key。isolated=True 的子作业不注册，杜绝 job 套 job。
        if request.workflow_mode and not request.isolated and _sbx_sess:
            from core.llm.tools.job_tool import register_run_job

            register_run_job(
                toolkit,
                user_id=request.current_user_id or "",
                chat_id=request.chat_id,
                sandbox_session_id=_sbx_sess,
                allowed_tools=sorted(enabled_mcp_keys or []),
                model_name=request.model_name,
                model_provider_id=request.model_provider_id,
            )

        # ── Phase 3.9: get_data_context (the "data dictionary" tool for direct-DB data retrieval) ──
        # Three gates combined: (1) a direct DB server is enabled this run
        # (db_query / es_query); (2) the external NL2SQL black box is excluded
        # (query_database isn't in the set, so it naturally doesn't trigger);
        # (3) the corresponding data source has annotation content. If any is
        # unmet, don't attach — avoid adding a useless tool that misleads the
        # model. The metadata only ever appears as a tool return value, never in
        # the system prompt. See db_metadata_service.
        _db_servers_on = {"db_query", "es_query"} & set(enabled_mcp_keys)
        if _db_servers_on:
            try:
                from core.services import db_metadata_service as _dbmeta

                _eligible_ds = await asyncio.to_thread(
                    _dbmeta.eligible_datasource_ids, _db_servers_on
                )
            except Exception as _e:  # noqa: BLE001
                _eligible_ds = []
                _log.warning("[factory] eligible_datasource_ids failed: %s", _e)
            if _eligible_ds:
                register_get_data_context(toolkit, _eligible_ds)

    if not request.disable_tools and _eval_scope is not None:
        from core.llm.evaluation_tools import register_evaluation_tools
        from core.vision import resolve_vision_mode

        register_evaluation_tools(
            toolkit,
            _eval_scope,
            read_only=request.read_only,
            allow_bash=request.allow_bash,
            vision_mode=resolve_vision_mode(request.model_provider_id or ""),
        )

    if not request.disable_tools and _api_scope is not None:
        from core.llm.agent_api_tools import register_agent_api_tools

        register_sandboxed_view_text_file(
            toolkit,
            allowed_skill_dirs,
            loader,
            loaded_skill_ids=loaded_skill_ids,
        )
        register_agent_api_tools(
            toolkit,
            _api_scope,
            read_only=request.read_only or not request.allow_bash,
            skill_dirs={sid: loader.get_skill_dir(sid) for sid in skill_ids_to_register or []},
        )

    return None
