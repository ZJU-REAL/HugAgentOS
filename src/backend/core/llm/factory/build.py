"""Build an agent through ordered capability, tool, prompt, and runtime phases."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from agentscope.agent import Agent
from agentscope.mcp import MCPClient
from core.llm.factory.assembly import assemble_agent
from core.llm.factory.request import AgentRequest
from core.llm.mcp_manager import close_factory_clients_on_error
from orchestration.registry import AgentSpec


@close_factory_clients_on_error
async def create_agent_executor(
    agent_spec: Optional[AgentSpec] = None,
    user_query: Optional[str] = None,
    disable_tools: bool = False,
    enabled_skill_ids: Optional[list[str]] = None,
    enabled_mcp_ids: Optional[list[str]] = None,
    enabled_kb_ids: Optional[list[str]] = None,
    current_user_id: Optional[str] = None,
    reranker_enabled: bool = False,
    model_name: Optional[str] = None,
    model_provider_id: Optional[str] = None,
    chat_mode: Optional[str] = None,
    memory_enabled: bool = False,
    user_agent: Optional[Any] = None,
    visible_subagents: Optional[List[Dict[str, Any]]] = None,
    isolated: bool = False,
    max_iters: Optional[int] = None,
    plan_mode: bool = False,
    # model_role: 指定按「模型管理 → 角色分配」的哪个角色解析默认模型（如
    # "loop_reviewer"）。优先级低于用户显式选择的 model_provider_id/model_name，
    # 高于 main_agent 兜底；plan_mode=True 等价于 model_role="plan_agent"。
    model_role: Optional[str] = None,
    batch_mode: bool = False,
    # workflow_mode: 工作流模式（用户显式触发：斜杠命令 /workflow 或 + 菜单选「工作流模式」）。
    # 只有它为 True 才注册 run_job 并注入作业脚本写法——与计划模式/批量执行同属"用户触发的
    # 模式"，不触发就完全不存在，普通问答不会被无关的批量规则干扰。
    workflow_mode: bool = False,
    # top_level_chat: whether this construction is a "top-level interactive main
    # conversation capable of hosting plan mode" — astream_chat_workflow passes
    # True explicitly after determining (has chat_id, not
    # channel/automation/batch/plan_chat). The update_plan tool is
    # registered ONLY on this positive signal. All derived/non-interactive paths
    # (plan generation, plan-execute steps, subagents, batch, autonomous loop,
    # channels, non-streaming…) default to False → they naturally never get the
    # tool, eliminating "plan within plan" nesting and all kinds of context
    # leaks at the root.
    top_level_chat: bool = False,
    chat_id: Optional[str] = None,
    # run_id lets the Runtime Binder (GCE ticket 03) key this run's frozen asset
    # bundle, so evidence assembled after the response can look up exactly which
    # versions were in play. Optional — non-chat paths simply bind anonymously.
    run_id: Optional[str] = None,
    journal_owner: Optional[str] = None,
    capability_scope: str = "",
    # Workspace scope is part of the frozen memory-policy ref and execution
    # context hash. Keep the default for non-chat/internal callers.
    workspace_id: str = "default",
    sandbox_session_id: Optional[str] = None,
    project_ctx: Optional[Dict[str, Any]] = None,
    channel_origin: Optional[Dict[str, Any]] = None,
    automation_run: bool = False,
    # read_only: read-only agent (for reviewers/auditors) — registers no
    # file-mutating tools (edit/write/delete/move/mkdir/myspace writes/
    # put_artifact), keeping only read/glob/grep/view/get_artifact. Callers may
    # independently disable Bash for a hard read-only boundary.
    read_only: bool = False,
    allow_bash: bool = True,
    # approval_mode: 用户自选的权限档（core.llm.tool_permissions 的 ask / auto /
    # full）。auto 让普通写入不再弹确认框、删除等危险操作照旧问；full 一律不问。
    # 留空表示"照用户自己存的那一档"——子智能体、计划步骤这些新起 agent 的入口
    # 不必逐个透传，也就不会漏掉一个就悄悄退回逐项确认。
    approval_mode: Optional[str] = None,
    # turbo_mode: 极速模式（quick-lookup entry）。Retrieval-only assembly:
    # carries exactly the admin-configured turbo MCP set（系统配置「极速模式」，
    # deliberately independent of catalog enable/disable state）, drops skills,
    # sandbox/file tools, subagents and plan tooling, swaps in the standalone
    # "turbo" system prompt, and hard-caps react iterations so the agent
    # answers within 1-2 (parallel) tool rounds. The two turbo_explicit_*
    # params are the only pass-throughs: capabilities the user explicitly
    # summoned this turn (slash skill / plugin expansion); the caller likewise
    # narrows visible_subagents to the @-mentioned agent only.
    turbo_mode: bool = False,
    turbo_explicit_skill_ids: Optional[List[str]] = None,
    turbo_explicit_mcp_ids: Optional[List[str]] = None,
    # invoked_skill_ids / invoked_mcp_ids: capabilities the user explicitly
    # summoned THIS turn（斜杠技能 / 插件 chip 展开的 skill_ids/mcp_ids），mode
    # 无关。Consumed by progressive plugin loading: an explicitly invoked
    # plugin must not be deferred (its user-message injection promises the
    # capability is active), and the invocation is persisted as a sticky
    # activation for the chat. turbo_explicit_* remain the turbo-only narrowing
    # pass-throughs.
    invoked_skill_ids: Optional[List[str]] = None,
    invoked_mcp_ids: Optional[List[str]] = None,
    # required_mcp_ids: connectors explicitly selected in the composer. Unlike
    # plugin MCP activation (available on demand), these IDs carry a fail-closed
    # contract: the first model round must execute one of their real tools.
    required_mcp_ids: Optional[List[str]] = None,
    # required_skill_*: the exact skill selected through the slash picker. It
    # must be loaded through view_text_file before the model can finish.
    required_skill_id: Optional[str] = None,
    required_skill_name: Optional[str] = None,
    # required_plugin_*: authoritative components of the plugin explicitly
    # selected this turn. Unlike ordinary activation, this is a real-use
    # contract: the model must read one of the plugin's SKILL.md files or call
    # one of its MCP tools before it may finish the answer.
    required_plugin_id: Optional[str] = None,
    required_plugin_name: Optional[str] = None,
    required_plugin_skill_ids: Optional[List[str]] = None,
    required_plugin_mcp_ids: Optional[List[str]] = None,
    # mode_spec: 对话模式的装配契约（core/services/chat_mode_service.ChatModeSpec）。
    # 「模式」把原来写死的极速模式泛化成一张表：工具面 / 技能 / 插件 / 提示词 kind /
    # 迭代上限都由它给。turbo_mode 现在的含义是"这个模式要收窄工具面"，收窄成什么
    # 由 mode_spec 说了算。缺省（老调用方没传）时退回历史的 turbo.* 配置键读取，
    # 保证升级期间不 regress。
    mode_spec: Optional[Any] = None,
    ontology_runtime: Optional[Dict[str, Any]] = None,
    # Per-tool-result context cap override (tokens). The 20k default suits
    # exploratory chat, but document-heavy workloads (autonomous-loop workers
    # polishing a 180k-char report) re-send every accumulated tool result on
    # each ReAct round — with 15-20 rounds/iteration that grows quadratically
    # to ~1M tokens per iteration. Loop callers pass a tighter cap; the
    # offloader keeps full content readable in the tool workspace .offload directory.
    tool_result_limit: Optional[int] = None,
    agent_api_scope: Optional[Dict[str, Any]] = None,
) -> Tuple[Agent, List[MCPClient]]:
    request = AgentRequest(
        agent_spec=agent_spec,
        user_query=user_query,
        disable_tools=disable_tools,
        enabled_skill_ids=enabled_skill_ids,
        enabled_mcp_ids=enabled_mcp_ids,
        enabled_kb_ids=enabled_kb_ids,
        current_user_id=current_user_id,
        reranker_enabled=reranker_enabled,
        model_name=model_name,
        model_provider_id=model_provider_id,
        chat_mode=chat_mode,
        memory_enabled=memory_enabled,
        user_agent=user_agent,
        visible_subagents=visible_subagents,
        isolated=isolated,
        max_iters=max_iters,
        plan_mode=plan_mode,
        model_role=model_role,
        batch_mode=batch_mode,
        workflow_mode=workflow_mode,
        top_level_chat=top_level_chat,
        chat_id=chat_id,
        run_id=run_id,
        journal_owner=journal_owner,
        capability_scope=capability_scope,
        workspace_id=workspace_id,
        sandbox_session_id=sandbox_session_id,
        project_ctx=project_ctx,
        channel_origin=channel_origin,
        automation_run=automation_run,
        read_only=read_only,
        allow_bash=allow_bash,
        approval_mode=approval_mode,
        turbo_mode=turbo_mode,
        turbo_explicit_skill_ids=turbo_explicit_skill_ids,
        turbo_explicit_mcp_ids=turbo_explicit_mcp_ids,
        invoked_skill_ids=invoked_skill_ids,
        invoked_mcp_ids=invoked_mcp_ids,
        required_mcp_ids=required_mcp_ids,
        required_skill_id=required_skill_id,
        required_skill_name=required_skill_name,
        required_plugin_id=required_plugin_id,
        required_plugin_name=required_plugin_name,
        required_plugin_skill_ids=required_plugin_skill_ids,
        required_plugin_mcp_ids=required_plugin_mcp_ids,
        mode_spec=mode_spec,
        ontology_runtime=ontology_runtime,
        tool_result_limit=tool_result_limit,
        agent_api_scope=agent_api_scope,
    )
    return await assemble_agent(request)
