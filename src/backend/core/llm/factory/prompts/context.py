"""Agent assembly phase: prompt context. """

from __future__ import annotations

import asyncio

from core.llm.factory.prompts import policy as factory_prompt_policy
from core.llm.factory.request import AgentRequest


async def prompt_context(
    request: AgentRequest,
    *,
    _api_scope,
    _elapsed,
    _eval_scope,
    _log,
    _manifest_builder,
    _sbx_sess,
    profile,
    system_prompt,
):
    from core.config.local_mode import local_mode_enabled

    if (
        _api_scope is None
        and _eval_scope is None
        and local_mode_enabled()
        and not any(
            section.id in {"runtime/environment", "runtime/local_mode"}
            for section, _ in _manifest_builder.prompt_section_sources()
        )
    ):
        from datetime import datetime

        from prompts.desktop_workspace import build_environment_context, build_local_mode_guidance

        environment = build_environment_context(
            {
                **(request.project_ctx or {}),
                "chat_id": request.chat_id,
                "sandbox_session_id": _sbx_sess,
                "approval_mode": request.approval_mode,
            },
            current_date=datetime.now().strftime("%Y-%m-%d"),
        )
        for section_id, content in (
            ("runtime/environment", environment),
            ("runtime/local_mode", build_local_mode_guidance()),
        ):
            system_prompt += "\n\n" + content
            _manifest_builder.add_prompt_section(
                section_id,
                content,
                origin="builtin:local_mode",
                trust="platform",
                priority=950,
                cache_class="workspace",
                version="1",
                sensitive=True,
            )

    # Prompt fragments this task type carries, per the active profile.
    #
    # This is the structural answer to a measured failure: giving a rule a scope
    # *in prose* does not work — the model either ignores the exception or
    # over-applies it, and both were observed against a real model. A fragment
    # attached to a profile reaches only the task types that profile governs, so
    # the scope is enforced by what is assembled rather than by what the text
    # asks the model to infer.
    if profile.prompt_fragments:
        fragments = await asyncio.to_thread(
            factory_prompt_policy._resolve_prompt_fragments, profile.prompt_fragments
        )
        if fragments:
            _dynamic_block = factory_prompt_policy._render_dynamic_block(fragments)
            system_prompt = system_prompt + "\n\n" + _dynamic_block
            _manifest_builder.add_prompt_section(
                "runtime/evolved_fragments",
                _dynamic_block,
                origin="evolution:prompt_fragments",
                trust="governed_runtime",
                priority=920,
                cache_class="profile",
                version=str(profile.version),
                reference=f"profile:{profile.profile_id}",
                sensitive=True,
            )
            _log.info(
                "[factory] +%s %d profile prompt fragment(s) appended",
                _elapsed(),
                len(fragments),
            )

    if _eval_scope is not None:
        _eval_guidance = (
            "This is a benchmark attempt in one leased OpenSandbox container. "
            "Native Bash, Read, Write, Edit, Glob, Grep and child agents share that container. "
            "Use the task's requested paths. Relative file paths use /workspace. "
            "Account files, credentials, history, memory and external connectors are unavailable. "
            "Leave outputs at the requested task paths for the verifier; do not publish artifacts."
        )
        system_prompt += "\n\n" + _eval_guidance
        _manifest_builder.add_prompt_section(
            "runtime/evaluation",
            _eval_guidance,
            origin="builtin:evaluation",
            trust="platform",
            priority=980,
            cache_class="capability_set",
            version="1",
        )

    if _api_scope is not None:
        _api_guidance = (
            "当前是子智能体专属 API 会话，只能使用本智能体绑定的能力及当前会话产物。"
            "不提供账号的个人记忆、其他会话、个人空间或个人登录凭据。"
            "Bash 和文件沙箱操作需要独立容器；不可用时如实说明，不尝试主机命令或其他路径。"
            "生成文件后用 sandbox_get_artifact 登记，再用 pin_to_workspace 交付文件 ID。"
        )
        system_prompt += "\n\n" + _api_guidance
        _manifest_builder.add_prompt_section(
            "runtime/agent_api",
            _api_guidance,
            origin="builtin:agent_api",
            trust="platform",
            priority=980,
            cache_class="capability_set",
            version="1",
        )

    return (system_prompt,)
