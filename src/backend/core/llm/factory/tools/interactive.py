"""Agent assembly phase: interactive tools. """

from __future__ import annotations

from typing import Optional

from core.llm.factory.request import AgentRequest
from core.llm.tools import register_read_artifact, register_sandboxed_view_text_file
from core.llm.tools._common import resolve_sandbox_session


async def interactive_tools(
    request: AgentRequest,
    *,
    _api_scope,
    _elapsed,
    _eval_scope,
    _log,
    _turbo_code_exec,
    allowed_skill_dirs,
    loader,
    skill_ids_to_register,
    toolkit,
):
    _log.info("[factory] +%s skills registered", _elapsed())

    from core.services.project_scope import project_scope_from_context

    _proj_scope = project_scope_from_context(request.project_ctx or {})

    loaded_skill_ids: set[str] = set()
    # Effective sandbox session: callers may pass an explicit id to layer sessions
    # (main/plan execution → chat_id persistent kernel; batch/subagent → "" ephemeral).
    # ``None`` means "not specified" → fall back to chat_id (legacy behavior).
    _sbx_sess: Optional[str] = resolve_sandbox_session(request.sandbox_session_id, request.chat_id)
    # Interactive mode = a human is in the loop and confirmations can be shown
    # (main/plan-execute). Batch items / subagents (isolated/batch) have no
    # human in the loop → non-interactive, and §13 rejects /myspace writes
    # outright.
    _interactive: bool = not (request.isolated or request.batch_mode or _api_scope or _eval_scope)

    # Browser-backed model questions are deliberately a top-level standard-chat
    # capability. Channels, automation, batch/plan workers and subagents have no
    # resident composer to answer them; turbo mode keeps its bounded fast path.
    from core.llm.tools import ask_user_question_tool

    if ask_user_question_tool.should_register_ask_user_question(
        top_level_chat=request.top_level_chat,
        turbo_mode=request.turbo_mode,
        disable_tools=request.disable_tools,
        chat_id=request.chat_id,
    ):
        ask_user_question_tool.register_ask_user_question(
            toolkit,
            chat_id=request.chat_id,
            interactive=True,
        )

    if not request.disable_tools and (request.project_ctx or {}).get("project_id"):
        from core.llm.tools.project_instructions_tool import (
            project_instruction_path,
            register_project_instruction_tools,
        )

        instruction_path = project_instruction_path(request.project_ctx)
        register_project_instruction_tools(
            toolkit,
            project_id=request.project_ctx["project_id"],
            user_id=request.current_user_id,
            allow_write=bool(request.project_ctx.get("project_init")) and not request.read_only,
            local_path=(
                request.project_ctx.get("project_local_path")
                if request.project_ctx.get("project_is_local")
                else None
            ),
            instruction_path=instruction_path,
        )

    # ── 跨会话历史（list_related_chats / read_chat） ──
    # 只读、按 user_id 锁死作用域，注册在收窄/标准两条路之前：用户可以在任何模式下把
    # 一段旧会话引用进来，注入的名片明确要求「细节去 read_chat 取」，模式收窄了工具却
    # 不在，等于让模型对着一张读不开的名片作答。
    if (
        not request.disable_tools
        and request.current_user_id
        and _api_scope is None
        and _eval_scope is None
    ):
        from core.llm.tools import register_chat_history_tools

        register_chat_history_tools(
            toolkit,
            user_id=str(request.current_user_id),
            chat_id=request.chat_id,
            project_id=(request.project_ctx or {}).get("project_id"),
        )

    if (
        not request.disable_tools
        and request.turbo_mode
        and not _turbo_code_exec
        and _api_scope is None
        and _eval_scope is None
    ):
        # Turbo keeps only cross-turn attachment access (the file-context hook
        # references this tool for historical attachments); every other native
        # tool — sandbox/Bash/file ops — is out of scope for quick lookup.
        register_read_artifact(toolkit, user_id=request.current_user_id)
        if skill_ids_to_register:
            # An explicitly summoned skill needs its SKILL.md readable (the
            # user-message injection tells the model to view_text_file it).
            # Bash/sandbox stay off: a skill that requires code execution gets
            # explained with a mode-switch suggestion, not executed (see the
            # turbo prompt).
            register_sandboxed_view_text_file(
                toolkit,
                allowed_skill_dirs,
                loader,
                loaded_skill_ids=loaded_skill_ids,
            )

    return (
        _interactive,
        _proj_scope,
        _sbx_sess,
        loaded_skill_ids,
    )
