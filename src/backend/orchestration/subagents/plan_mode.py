"""Plan mode orchestration — generate and execute structured plans.

Phase 1 (generate): AI analyzes a task description and produces a structured plan.
Phase 2 (execute):  Steps are executed sequentially, each with its own agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from core.config.settings import DEFAULT_CHAT_MODEL_ALIAS
from core.db.models import Plan
from core.services import log_service as log_writer
from core.infra.logging import LogContext
from core.llm.agent_factory import create_agent_executor
from core.llm.mcp_manager import close_clients
from core.services.plan_service import PlanService
from orchestration.chat_run_executor import is_run_cancelled
from orchestration.streaming import StreamingAgent
from orchestration.subagents.plugin_visibility import (
    all_plugin_component_ids as _all_plugin_component_ids,
    load_enabled_plugins as _load_enabled_plugins,
)

import time as _time

logger = logging.getLogger(__name__)

# Path to the plan-mode system prompt (fallback when DB has no plan_mode version)
_PROMPT_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "prompts",
    "prompt_text",
    "plan_mode",
    "plan_mode.system.md",
)


def _collect_valid_tool_names(enabled_mcp_ids: Optional[List[str]] = None) -> Optional[set]:
    """Collect all valid MCP tool function names from enabled servers.

    Returns None if no filter is applied (all tools valid),
    or a set of valid tool names + server IDs.
    """
    if enabled_mcp_ids is None:
        return None
    valid = set(enabled_mcp_ids)  # server IDs themselves are valid references
    try:
        from core.services.mcp_service import McpServerConfigService

        svc = McpServerConfigService.get_instance()
        all_servers = svc.get_all_servers()
        for sid in enabled_mcp_ids:
            cfg = all_servers.get(sid, {})
            for tool in cfg.get("tools_json", []) or []:
                if isinstance(tool, dict) and tool.get("name"):
                    valid.add(tool["name"])
    except Exception:
        pass
    return valid


def _load_visible_agents(
    db: Session,
    user_id: str,
    enabled_agent_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Load visible sub-agents for the user, optionally filtered by IDs."""
    try:
        from core.services.user_agent_service import UserAgentService

        svc = UserAgentService(db)
        agents = svc.list_for_user(user_id)
        # Only include enabled agents
        agents = [a for a in agents if a.get("is_enabled", True)]
        # Filter by enabled_agent_ids if provided
        if enabled_agent_ids is not None:
            id_set = set(enabled_agent_ids)
            agents = [a for a in agents if a.get("agent_id") in id_set]
        return agents
    except Exception as exc:
        logger.warning("Failed to load visible agents: %s", exc)
        return []


def _resolve_plugin_capabilities(
    db: Session,
    user_id: str,
    enabled_skill_ids: Optional[List[str]],
    enabled_mcp_ids: Optional[List[str]],
) -> Tuple[Optional[List[str]], Optional[List[str]], List[Dict[str, Any]]]:
    """Merge the user's enabled **plugin components** into the enabled skill / MCP sets, and return active plugin metadata.

    Plan mode's caller (the frontend) takes the enabled lists from
    ``catalog.skills`` / ``catalog.mcp``, and those lists do **not** contain
    plugin-packaged components (see ``api/routes/v1/catalog.py``). Main chat goes
    through the server-side ``resolve_all_runtime_enabled`` which naturally includes
    plugins; this function brings plan mode up to the same capability level.

    - ``enabled_*`` is a list (interactive) → only **append** the currently enabled
      plugin components (leave the user's choices on regular catalog items alone,
      preserving strict override semantics).
    - ``enabled_*`` is None (automation) → keep None; each step's ``_merge_enabled``
      falls back to the plan-declared expected_* (the generation phase already wrote
      the plugin components into it).
    """
    try:
        from core.config.catalog_resolver import resolve_all_runtime_enabled

        r_skills, _r_agents, r_mcps = resolve_all_runtime_enabled(db, user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("plan: resolve_all_runtime_enabled failed: %s", exc)
        r_skills, r_mcps = None, None

    plug_skill_all, plug_mcp_all = _all_plugin_component_ids(db, user_id)
    enabled_plug_skills = plug_skill_all & set(r_skills or [])
    enabled_plug_mcps = plug_mcp_all & set(r_mcps or [])

    def _merge(passed: Optional[List[str]], extra: set) -> Optional[List[str]]:
        if passed is None:
            return None
        if not extra:
            return list(passed)
        return sorted(set(passed) | extra)

    eff_skills = _merge(enabled_skill_ids, enabled_plug_skills)
    eff_mcps = _merge(enabled_mcp_ids, enabled_plug_mcps)
    plugins = _load_enabled_plugins(db, user_id, enabled_plug_skills, enabled_plug_mcps)
    return eff_skills, eff_mcps, plugins


def _load_plan_prompt(available_tools_desc: str = "（暂无工具信息）") -> str:
    """Load and render the plan-mode system prompt.

    Resolution order:
    1. Active "plan_mode" version in the Config version-management pool
    2. Legacy: active "system" version's part "system/90_plan_mode"
       (pre-migration fallback)
    3. Filesystem file prompts/prompt_text/plan_mode/plan_mode.system.md
    4. Hardcoded minimal fallback
    """
    template: Optional[str] = None

    try:
        from core.services import prompt_version_service as pvs

        rendered = pvs.render_active_prompt("plan_mode")
        if rendered and rendered.strip():
            template = rendered
        else:
            # Legacy path: some system versions may still carry system/90_plan_mode
            av = pvs.get_active_version("system")
            if av:
                for p in av.get("parts") or []:
                    if (p.get("part_id") or "").strip() == "system/90_plan_mode" and p.get(
                        "is_enabled", True
                    ):
                        content = p.get("content") or ""
                        if content.strip():
                            template = content
                            break
    except Exception as exc:
        logger.debug("plan_mode: prompt_version_service miss (%s)", exc)

    if template is None:
        try:
            path = os.path.normpath(_PROMPT_PATH)
            with open(path, "r", encoding="utf-8") as f:
                template = f.read()
        except Exception as exc:
            logger.warning("Failed to load plan prompt: %s", exc)
            return (
                "你是一个任务分解助手。请将用户的任务分解为可执行步骤，"
                '输出严格JSON格式：{"title": "...", "description": "...", '
                '"steps": [{"title": "...", "description": "...", "expected_tools": [...]}]}'
            )

    return template.replace("{available_tools}", available_tools_desc)


def _build_tools_description(
    enabled_mcp_ids: Optional[List[str]] = None,
    enabled_skill_ids: Optional[List[str]] = None,
    visible_agents: Optional[List[Dict[str, Any]]] = None,
    plugins: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build a description of available MCP tools, skills, sub-agents, and plugins.

    Returns clearly separated sections so the AI can distinguish
    between MCP tools (expected_tools), skills (expected_skills),
    and sub-agents (expected_agents). Plugins are shown as capability
    bundles whose packaged skills/MCP go into expected_skills/expected_tools.
    """
    tool_lines: List[str] = []
    skill_lines: List[str] = []
    agent_lines: List[str] = []
    plugin_lines: List[str] = []

    # MCP tools — only include those in the enabled list
    # enabled_mcp_ids=None means "no filter" (all), [] means "none enabled"
    try:
        from core.services.mcp_service import McpServerConfigService

        svc = McpServerConfigService.get_instance()
        all_servers = svc.get_all_servers()  # returns dict {server_id: config}
        mcp_filter = set(enabled_mcp_ids) if enabled_mcp_ids is not None else None
        for sid, s in all_servers.items():
            if mcp_filter is not None and sid not in mcp_filter:
                continue
            name = s.get("display_name", sid)
            desc = s.get("description", "")
            tools = s.get("tools_json", [])
            tool_names = [t.get("name", "") for t in tools if isinstance(t, dict)] if tools else []
            tool_lines.append(f"- **{name}** ({sid}): {desc}")
            if tool_names:
                tool_lines.append(f"  具体工具函数: {', '.join(tool_names)}")
    except Exception as exc:
        logger.warning("Failed to load MCP tools: %s", exc)

    # Skills — only include those in the enabled list
    # enabled_skill_ids=None means "no filter" (all), [] means "none enabled"
    try:
        from core.agent_skills.loader import get_skill_loader

        loader = get_skill_loader()
        all_skills = loader.load_all_metadata()
        skill_filter = set(enabled_skill_ids) if enabled_skill_ids is not None else None
        for skill_id, meta in all_skills.items():
            if skill_filter is not None and skill_id not in skill_filter:
                continue
            name = (
                getattr(meta, "name", skill_id)
                if not isinstance(meta, dict)
                else meta.get("name", skill_id)
            )
            desc = (
                getattr(meta, "description", "")
                if not isinstance(meta, dict)
                else meta.get("description", "")
            )
            skill_lines.append(f"- **{name}** (id: {skill_id}): {desc}")
    except Exception as exc:
        logger.warning("Failed to load skills: %s", exc)

    if visible_agents:
        from core.llm.subagent_tool import _get_tools_desc

        for a in visible_agents:
            agent_id = a.get("agent_id", "")
            name = a.get("name", agent_id)
            desc = a.get("description", "")
            agent_lines.append(
                f"- **{name}** (id: {agent_id}): {desc}（可用工具: {_get_tools_desc(a)}）"
            )

    # Plugins — capability bundles; their packaged skills/MCP are already merged
    # into the enabled skill/mcp sets above, so they also appear in those
    # sections. This block groups them by plugin so the AI understands the
    # bundle and picks the right component id for expected_skills/expected_tools.
    if plugins:
        for p in plugins:
            parts = []
            if p.get("skill_ids"):
                parts.append(f"技能 {', '.join(p['skill_ids'])}（填 expected_skills）")
            if p.get("mcp_ids"):
                parts.append(f"MCP {', '.join(p['mcp_ids'])}（填 expected_tools）")
            bundle = "；".join(parts) if parts else "无可用组件"
            plugin_lines.append(
                f"- **{p.get('name', '')}**：{p.get('description', '')}\n  打包能力：{bundle}"
            )

    sections = []
    if tool_lines:
        sections.append("### MCP 工具（填入 expected_tools 字段）\n" + "\n".join(tool_lines))
    if skill_lines:
        sections.append("### 技能（填入 expected_skills 字段）\n" + "\n".join(skill_lines))
    if agent_lines:
        sections.append("### 子智能体（填入 expected_agents 字段）\n" + "\n".join(agent_lines))
    if plugin_lines:
        sections.append(
            "### 插件（能力包，其打包的技能填 expected_skills、MCP 填 expected_tools）\n"
            + "\n".join(plugin_lines)
        )

    return "\n\n".join(sections) if sections else "（当前无可用工具或技能）"


async def _prepare_history(
    session_messages: List[Dict[str, Any]],
    model_name: str,
) -> List[Dict[str, Any]]:
    """Trim history to fit context window.

    History comes from the main conversation's checkpoint-aware replay
    (cross-turn/PreTurn compaction has already covered it); going over budget here
    is rare, so we simply trim to budget (no on-the-spot summarization anymore).
    """
    if not session_messages:
        return []
    from core.llm.context_manager import ContextWindowManager

    ctx_mgr = ContextWindowManager.for_model(model_name)
    trimmed = ctx_mgr.trim_history(session_messages)
    dropped_count = len(session_messages) - len(trimmed)
    if dropped_count > 0:
        logger.warning("[plan_mode] context over budget: dropped %d message(s)", dropped_count)
    return trimmed


def _build_file_context(uploaded_files: List[Dict[str, Any]], max_chars: int = 50000) -> str:
    """Build file context text from uploaded file attachments."""
    if not uploaded_files:
        return ""
    file_names = [f"- {f.get('name', '未知文件')}" for f in uploaded_files]
    content_parts: List[str] = []
    for f in uploaded_files:
        content = (f.get("content") or "").strip()
        if content:
            name = f.get("name", "未知文件")
            if len(content) > max_chars:
                content = content[:max_chars] + "\n... (内容过长，已截断)"
            content_parts.append(f"### {name}\n{content}")
    if not content_parts:
        return ""
    return (
        f"[附件文件]: {chr(10).join(file_names)}\n\n"
        f"[附件内容]\n" + "\n\n---\n\n".join(content_parts) + "\n[附件内容结束]"
    )


async def astream_generate_plan(
    task_description: str,
    user_id: str,
    db: Session,
    model_name: str = DEFAULT_CHAT_MODEL_ALIAS,
    model_provider_id: Optional[str] = None,
    enabled_mcp_ids: Optional[List[str]] = None,
    enabled_skill_ids: Optional[List[str]] = None,
    enabled_kb_ids: Optional[List[str]] = None,
    enabled_agent_ids: Optional[List[str]] = None,
    session_messages: Optional[List[Dict[str, Any]]] = None,
    uploaded_files: Optional[List[Dict[str, Any]]] = None,
    chat_id: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """Phase 1: Generate a structured plan from a task description.

    Yields SSE events:
    - plan_generating  {delta: str}
    - plan_generated   {plan_id, title, description, steps: [...]}
    - plan_error       {error: str}
    """
    visible_agents = _load_visible_agents(db, user_id, enabled_agent_ids)

    # Merge enabled plugin components into the enabled skill / MCP sets (the frontend
    # catalog lists don't contain plugin components), so plan mode's available
    # capabilities match main chat, and fetch active plugin metadata for prompt display.
    enabled_skill_ids, enabled_mcp_ids, plugins = _resolve_plugin_capabilities(
        db, user_id, enabled_skill_ids, enabled_mcp_ids
    )

    tools_desc = _build_tools_description(
        enabled_mcp_ids, enabled_skill_ids, visible_agents, plugins
    )
    system_prompt = _load_plan_prompt(tools_desc)

    # Project context: when the chat is attached to a project, inject the project's
    # instructions / file listing into the plan-generation agent's system prompt so
    # the planning phase already understands the project context.
    from core.services.project_scope import build_project_ctx_from_chat_id

    _project_ctx = build_project_ctx_from_chat_id(db, chat_id)
    from core.services.ontology_service import build_user_ontology_runtime

    ontology_enabled, ontology_runtime = build_user_ontology_runtime(
        user_id=user_id,
        task=task_description,
        db=db,
    )

    try:
        # ``enabled_skill_ids=[]`` keeps the factory from falling back to all
        # main-agent skills — otherwise the JSON-only plan generator gets a
        # toolkit + skill-loading instructions and starts running tools.
        agent, mcp_clients = await create_agent_executor(
            disable_tools=True,
            enabled_skill_ids=[],
            model_name=model_name,
            model_provider_id=model_provider_id,
            chat_mode="fast",
            current_user_id=user_id,
            chat_id=chat_id,
            project_ctx=_project_ctx,
            ontology_runtime=ontology_runtime,
        )

        # Embed plan prompt as part of user message (avoid system message
        # conflict with factory's built-in system prompt).
        file_context = _build_file_context(uploaded_files or [])
        file_section = f"\n\n---\n\n{file_context}" if file_context else ""
        user_content = f"{system_prompt}\n\n---\n\n用户任务：{task_description}{file_section}"

        # Prepare history from chat session (trimmed to context budget)
        history = await _prepare_history(session_messages or [], model_name)
        logger.warning(
            "[plan-generate] prepared %d history msgs from %d session msgs",
            len(history),
            len(session_messages or []),
        )
        history.append({"role": "user", "content": user_content})

        streaming_agent = StreamingAgent(agent, mcp_clients)
        full_text = ""

        try:
            async for event_type, payload in streaming_agent.stream(
                history,
                {
                    "user_id": user_id,
                    "model_name": model_name,
                    "model_provider_id": model_provider_id or "",
                    "enable_thinking": False,
                    "chat_mode": "fast",
                    "ontology_enabled": ontology_enabled,
                    "ontology_runtime": ontology_runtime,
                },
            ):
                if event_type == "text_delta":
                    full_text += payload
                    yield {"type": "plan_generating", "delta": payload}
                elif event_type == "error":
                    yield {"type": "plan_error", "error": str(payload)}
                    return
                else:
                    logger.warning(
                        "[plan-generate] unexpected stream event '%s' (disable_tools=True)",
                        event_type,
                    )
        finally:
            await close_clients(mcp_clients)

        # Parse the generated plan JSON
        plan_data = _parse_plan_json(full_text)
        if not plan_data:
            yield {"type": "plan_error", "error": "AI 输出格式解析失败，请重试"}
            return

        _valid_tools = _collect_valid_tool_names(enabled_mcp_ids)
        _valid_skills = set(enabled_skill_ids) if enabled_skill_ids is not None else None
        _valid_agents = {a.get("agent_id") for a in visible_agents}
        for step_data in plan_data.get("steps", []):
            if _valid_tools is not None:
                step_data["expected_tools"] = [
                    t for t in (step_data.get("expected_tools") or []) if t in _valid_tools
                ]
            if _valid_skills is not None:
                step_data["expected_skills"] = [
                    s for s in (step_data.get("expected_skills") or []) if s in _valid_skills
                ]
            step_data["expected_agents"] = [
                a for a in (step_data.get("expected_agents") or []) if a in _valid_agents
            ]

        # Persist to DB
        svc = PlanService(db)
        plan = svc.create_plan(
            user_id=user_id,
            title=plan_data.get("title", "未命名计划"),
            description=plan_data.get("description", ""),
            task_input=task_description,
            steps=plan_data.get("steps", []),
        )

        agent_name_map = (
            {a.get("agent_id"): a.get("name", a.get("agent_id", "")) for a in visible_agents}
            if visible_agents
            else {}
        )

        extra = {}
        if uploaded_files:
            extra["uploaded_files"] = uploaded_files
        if agent_name_map:
            extra["agent_name_map"] = agent_name_map
        if extra:
            svc.update_plan(plan.plan_id, extra_data=extra)

        event = {"type": "plan_generated", **PlanService.plan_to_dict(plan)}
        if agent_name_map:
            event["agent_name_map"] = agent_name_map
        # Attach token usage from plan generation LLM call
        event["usage"] = streaming_agent.get_usage()
        yield event

    except Exception as exc:
        logger.exception("Plan generation failed")
        yield {"type": "plan_error", "error": f"计划生成失败: {exc}"}


def _parse_plan_json(text: str) -> Optional[Dict[str, Any]]:
    """Extract JSON plan from AI output, handling markdown fences."""
    text = text.strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code block
    import re

    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try finding JSON object boundaries
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    return None


async def astream_execute_plan(
    plan_id: str,
    user_id: str,
    db: Session,
    model_name: str = DEFAULT_CHAT_MODEL_ALIAS,
    enabled_mcp_ids: Optional[List[str]] = None,
    enabled_skill_ids: Optional[List[str]] = None,
    enabled_kb_ids: Optional[List[str]] = None,
    enabled_agent_ids: Optional[List[str]] = None,
    session_messages: Optional[List[Dict[str, Any]]] = None,
    uploaded_files: Optional[List[Dict[str, Any]]] = None,
    chat_id: Optional[str] = None,
    run_id: Optional[str] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """Phase 2: Execute a plan step by step.

    Yields SSE events:
    - plan_step_start     {step_id, step_order, title}
    - plan_step_progress  {step_id, delta: str}
    - tool_call           existing format + {step_id}
    - tool_result         existing format + {step_id}
    - plan_step_complete  {step_id, status, summary}
    - plan_error          {plan_id, step_id?, error}
    - plan_complete       {plan_id, status, summary, completed_steps, total_steps}
    """
    logger.warning("[plan-exec] astream_execute_plan called for plan_id=%s", plan_id)

    svc = PlanService(db)
    plan = svc.get_plan(plan_id, user_id)
    if not plan:
        yield {"type": "plan_error", "plan_id": plan_id, "error": "计划不存在"}
        return

    logger.warning("[plan-exec] plan status=%s, steps=%d", plan.status, len(plan.steps))
    if plan.status not in ("approved", "running"):
        yield {
            "type": "plan_error",
            "plan_id": plan_id,
            "error": f"计划状态 '{plan.status}' 不可执行",
        }
        return

    plan_meta: Dict[str, Any] = plan.extra_data if isinstance(plan.extra_data, dict) else {}
    if not uploaded_files:
        uploaded_files = plan_meta.get("uploaded_files")
    # chat_id falls back to the plan-creation chat so sandbox tools
    # (bash / put_artifact) can bind to a persistent /workspace session.
    if not chat_id:
        chat_id = plan_meta.get("chat_id") or None

    # Project context: when the chat is attached to a project, inject the project
    # instructions / files, and confine each step-execution agent's file scope to the
    # project folder (same ProjectScope source as regular chat).
    from core.services.project_scope import build_project_ctx_from_chat_id

    _project_ctx = build_project_ctx_from_chat_id(db, chat_id)

    # Merge enabled plugin components into the enabled sets (frontend catalog lists don't
    # contain plugin components) so each step-execution agent can load the plugin-packaged
    # skills / MCP — consistent with main chat and with the generation phase.
    enabled_skill_ids, enabled_mcp_ids, _plan_plugins = _resolve_plugin_capabilities(
        db, user_id, enabled_skill_ids, enabled_mcp_ids
    )

    svc.update_plan(plan_id, status="running")
    completed_count = 0
    cancelled = False

    _log_ctx = LogContext(user_id=user_id or None, chat_id=plan_meta.get("chat_id"))
    _log_ctx.__enter__()

    _plan_run_start = _time.monotonic()
    _plan_subagent_log_id = await log_writer.start_subagent_log(
        {
            "subagent_name": "plan_mode",
            "subagent_type": "plan_mode",
            "subagent_id": plan_id,
            "plan_id": plan_id,
            "model": model_name,
            "step_title": plan.title,
            "input_messages": {
                "task_input": plan.task_input,
                "total_steps": plan.total_steps,
                "enabled_mcp_ids": enabled_mcp_ids,
                "enabled_skill_ids": enabled_skill_ids,
                "enabled_agent_ids": enabled_agent_ids,
            },
        }
    )
    _plan_tool_count = 0
    _plan_skill_count_start = 0  # best-effort (we don't track skills here explicitly)

    # Prepare chat history once (trimmed to context budget) for all steps
    prepared_history = await _prepare_history(session_messages or [], model_name)
    logger.warning(
        "[plan-exec] prepared %d history msgs from %d session msgs",
        len(prepared_history),
        len(session_messages or []),
    )

    step_summaries: List[str] = []
    # Bounded rolling context fed into next step's instruction.
    _PER_STEP_OUTPUT_CHARS = 3000
    _MAX_PREVIOUS_OUTPUTS = 6
    step_outputs_for_context: List[str] = []
    last_step_text: str = ""

    # Plan header is identical across steps — build once, swap marker per step.
    goal_lines, roadmap_lines = _build_plan_header_lines(plan)
    # Keep ALL MCP clients alive to prevent GC-triggered cancel scope
    # crashes during plan execution. Terminate them only at the very end.
    _all_mcp_clients: List = []

    # Cache visible_agents lookup per effective-id-set to avoid N DB queries
    # across steps (agent visibility is user-scoped and stable within the run).
    _agent_cache: Dict[Optional[tuple], List[Dict[str, Any]]] = {}

    def _merge_enabled(
        global_list: Optional[List[str]], step_list: Optional[List[str]]
    ) -> Optional[List[str]]:
        """Strict runtime override.

        - global_list is None → caller has no runtime opinion; fall back to
          the plan-declared expected_* list (used by automation tasks that
          weren't configured with explicit IDs).
        - global_list is a list (including empty) → caller is explicit; use
          it as-is. We MUST NOT re-add plan-declared items, otherwise a
          tool/skill the user disabled in the catalog between generation
          and execution would silently come back.
        """
        if global_list is None:
            return list(step_list) if step_list else None
        return list(global_list)

    def _cancellation_requested() -> bool:
        if not run_id:
            return False
        try:
            return is_run_cancelled(run_id)
        except Exception:
            return False

    try:
        for step_idx, step in enumerate(plan.steps):
            logger.warning(
                "[plan-exec] === Step %d/%d: %s ===", step_idx + 1, len(plan.steps), step.title
            )
            if cancelled:
                svc.update_step(step.step_id, status="skipped")
                continue

            # Refresh plan to check for cancellation
            db.refresh(plan)
            if plan.status == "cancelled" or _cancellation_requested():
                cancelled = True
                svc.update_step(step.step_id, status="skipped")
                continue

            yield {
                "type": "plan_step_start",
                "step_id": step.step_id,
                "step_order": step.step_order,
                "title": step.title,
            }
            # Heartbeat to keep SSE alive during agent setup (3-10s)
            yield {"type": "heartbeat"}

            svc.update_step(step.step_id, status="running", started_at=datetime.utcnow())

            # Build step instruction with context from previous steps
            step_instruction = _build_step_instruction(
                step,
                step_summaries,
                goal_lines=goal_lines,
                roadmap_lines=roadmap_lines,
                previous_step_outputs=step_outputs_for_context,
            )

            step_text = ""
            step_tool_calls: List[Dict] = []

            _step_start_monotonic = _time.monotonic()
            _step_subagent_log_id = await log_writer.start_subagent_log(
                {
                    "subagent_name": f"plan_mode:step_{step.step_order}",
                    "subagent_type": "plan_step",
                    "subagent_id": step.step_id,
                    "plan_id": plan_id,
                    "step_id": step.step_id,
                    "step_index": step.step_order,
                    "step_title": step.title,
                    "model": model_name,
                    "parent_subagent_log_id": _plan_subagent_log_id,
                    "input_messages": {
                        "instruction": step_instruction,
                        "expected_tools": step.expected_tools,
                        "expected_skills": step.expected_skills,
                    },
                }
            )
            _step_outcome = "success"
            _step_error_msg: Optional[str] = None

            try:
                # Plan-declared expected_* is folded into the enabled_* set so
                # scheduled runs (which pass empty enabled lists) still load
                # the tools each step claimed it needs.
                step_mcp_ids = _merge_enabled(enabled_mcp_ids, step.expected_tools or [])
                step_skill_ids = _merge_enabled(enabled_skill_ids, step.expected_skills or [])
                step_agent_ids = _merge_enabled(enabled_agent_ids, step.expected_agents or [])

                _agent_key = tuple(step_agent_ids) if step_agent_ids is not None else None
                if _agent_key not in _agent_cache:
                    _agent_cache[_agent_key] = _load_visible_agents(db, user_id, step_agent_ids)
                step_visible_agents = _agent_cache[_agent_key]

                logger.warning(
                    "[plan-exec] step %d effective: mcp=%s skills=%s agents=%s",
                    step.step_order,
                    step_mcp_ids if step_mcp_ids is not None else "<all>",
                    step_skill_ids if step_skill_ids is not None else "<all>",
                    [a.get("agent_id") for a in step_visible_agents] if step_visible_agents else [],
                )

                # max_iters bounds tool calls per step so the agent doesn't loop excessively.
                _step_max_iters = int(os.environ.get("PLAN_STEP_MAX_ITERS", "5"))
                from core.services.ontology_service import build_user_ontology_runtime

                _step_ontology_enabled, step_ontology_runtime = build_user_ontology_runtime(
                    user_id=user_id,
                    task=f"{plan.task_input}\n{step_instruction}",
                    db=db,
                )
                agent, mcp_clients = await create_agent_executor(
                    enabled_mcp_ids=step_mcp_ids,
                    enabled_skill_ids=step_skill_ids,
                    enabled_kb_ids=enabled_kb_ids,
                    visible_subagents=step_visible_agents if step_visible_agents else None,
                    current_user_id=user_id,
                    model_name=model_name,
                    chat_id=chat_id,
                    max_iters=_step_max_iters,
                    project_ctx=_project_ctx,
                    ontology_runtime=step_ontology_runtime,
                    # top_level_chat not passed (defaults to False) → plan-execution steps
                    # inherently never get enter_plan_mode, ruling out "plan inside a plan"
                    # nesting (aligned with Claude Code: tool availability is pinned down by
                    # execution context before the run starts).
                )

                # AgentScope 2.0: no hooks dict; model switching is handled by
                # DynamicModelMiddleware. Usage accumulation used to go through
                # hook-patch + _UsageTrackingModel, which no longer applies in 2.0 —
                # plan steps run agent.reply() directly, so no usage proxying here
                # (a downgrade; could later subscribe to ModelCallEndEvent instead).

                # ── Direct agent.reply() with timeout + heartbeats ──
                from agentscope.message import Msg, TextBlock
                from core.llm.message_compat import session_to_msgs

                agent.state.context.extend(session_to_msgs(prepared_history))

                # Inject file context (content must be a list of blocks)
                file_context = _build_file_context(uploaded_files or [])
                if file_context:
                    agent.state.context.append(
                        Msg(
                            name="user",
                            role="user",
                            content=[TextBlock(type="text", text=file_context)],
                        )
                    )

                user_msg = Msg(
                    name="user",
                    role="user",
                    content=[TextBlock(type="text", text=step_instruction)],
                )

                yield {
                    "type": "plan_step_progress",
                    "step_id": step.step_id,
                    "delta": "正在执行...\n",
                }

                try:
                    # Run agent.reply in background, send heartbeats while waiting.
                    # Wrap the whole reply in a subagent scope so any tool_call_logs
                    # produced inside are attributed to this step.
                    with log_writer.subagent_scope(_step_subagent_log_id, source="subagent"):
                        reply_task = asyncio.create_task(agent.reply(inputs=user_msg))
                    while not reply_task.done():
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(reply_task),
                                timeout=15,
                            )
                        except asyncio.TimeoutError:
                            if _cancellation_requested():
                                logger.warning(
                                    "[plan-exec] step %d cancel requested; cancelling reply_task",
                                    step.step_order,
                                )
                                reply_task.cancel()
                                try:
                                    await reply_task
                                except (asyncio.CancelledError, Exception):
                                    pass
                                cancelled = True
                                break
                            yield {"type": "heartbeat"}
                        except asyncio.CancelledError:
                            raise

                    if cancelled:
                        step_text = "步骤执行被取消"
                        svc.update_step(
                            step.step_id,
                            status="skipped",
                            completed_at=datetime.utcnow(),
                        )
                        await log_writer.finish_subagent_log(
                            _step_subagent_log_id,
                            status="cancelled",
                            output_content=step_text,
                            duration_ms=int((_time.monotonic() - _step_start_monotonic) * 1000),
                        )
                        _all_mcp_clients.extend(mcp_clients)
                        yield {
                            "type": "plan_step_complete",
                            "step_id": step.step_id,
                            "status": "cancelled",
                            "summary": "已取消",
                        }
                        continue

                    reply = reply_task.result()

                    # ── Extract tool calls from agent memory ──
                    _pending_log: Dict[str, Dict[str, Any]] = {}
                    try:
                        # AgentScope 2.0: agent.memory → agent.state.context;
                        # tool_use → tool_call; content blocks are pydantic objects (attribute access).
                        mem_msgs = list(agent.state.context)
                        for mem_msg in mem_msgs or []:
                            if hasattr(
                                mem_msg, "has_content_blocks"
                            ) and mem_msg.has_content_blocks("tool_call"):
                                for block in mem_msg.get_content_blocks("tool_call"):
                                    tool_name = getattr(block, "name", "unknown")
                                    tool_id = getattr(block, "id", "")
                                    _raw_input = getattr(block, "input", "") or ""
                                    try:
                                        tool_args = (
                                            json.loads(_raw_input)
                                            if isinstance(_raw_input, str)
                                            else _raw_input
                                        )
                                    except (json.JSONDecodeError, ValueError):
                                        tool_args = {"_raw": _raw_input}
                                    # 2.0: block is a ToolCallBlock pydantic object, not JSON-serializable;
                                    # tool_calls_log is JSONB → must store a serializable dict (in 1.x it already was a dict).
                                    step_tool_calls.append(
                                        {
                                            "tool_name": tool_name,
                                            "tool_id": tool_id,
                                            "tool_args": tool_args,
                                        }
                                    )
                                    _pending_log[tool_id] = {
                                        "tool_name": tool_name,
                                        "tool_args": tool_args,
                                    }
                                    yield {
                                        "type": "tool_call",
                                        "step_id": step.step_id,
                                        "tool_name": tool_name,
                                        "tool_id": tool_id,
                                        "tool_args": tool_args,
                                    }
                            if hasattr(
                                mem_msg, "has_content_blocks"
                            ) and mem_msg.has_content_blocks("tool_result"):
                                for block in mem_msg.get_content_blocks("tool_result"):
                                    tool_name = getattr(block, "name", "unknown")
                                    tool_id = getattr(block, "id", "")
                                    output = getattr(block, "output", []) or []
                                    # Try to extract structured content; fall back to text
                                    content: Any = output
                                    if isinstance(output, list):
                                        # Single text block → string; otherwise keep as-is
                                        text_parts = []
                                        has_only_text = True
                                        for item in output:
                                            if isinstance(item, dict):
                                                text_val = item.get("text")
                                                if text_val is not None:
                                                    text_parts.append(str(text_val))
                                                else:
                                                    has_only_text = False
                                                    break
                                            elif isinstance(item, str):
                                                text_parts.append(item)
                                            elif getattr(item, "type", None) == "text":
                                                text_parts.append(getattr(item, "text", ""))
                                            else:
                                                has_only_text = False
                                                break
                                        if has_only_text and text_parts:
                                            # Try parsing as JSON for structured output
                                            joined = "\n".join(text_parts)
                                            try:
                                                content = json.loads(joined)
                                            except (json.JSONDecodeError, ValueError):
                                                content = joined
                                        # else: keep content = output (list)
                                    elif isinstance(output, str):
                                        try:
                                            content = json.loads(output)
                                        except (json.JSONDecodeError, ValueError):
                                            content = output
                                    try:
                                        _call = _pending_log.pop(tool_id, {})
                                        log_writer.schedule_tool_call_write(
                                            {
                                                # Pass user_id/chat_id explicitly: plan execution runs in a subtask
                                                # context where contextvars are unreliable (same fix as the streaming main path).
                                                "user_id": user_id or None,
                                                "chat_id": chat_id or None,
                                                "tool_name": _call.get("tool_name") or tool_name,
                                                "tool_call_id": tool_id,
                                                "tool_args": _call.get("tool_args"),
                                                "tool_result": content,
                                                "status": "success",
                                                "source": "subagent",
                                                "subagent_log_id": _step_subagent_log_id,
                                            }
                                        )
                                    except Exception:
                                        logger.debug("plan tool log failed", exc_info=True)
                                    # Backfill the result into the matching tool_call entry (for tool_calls_log persistence)
                                    for _e in step_tool_calls:
                                        if _e.get("tool_id") == tool_id and "result" not in _e:
                                            _e["result"] = content
                                            _e["status"] = "success"
                                            break
                                    yield {
                                        "type": "tool_result",
                                        "step_id": step.step_id,
                                        "tool_name": tool_name,
                                        "tool_id": tool_id,
                                        "result": content,
                                    }
                    except Exception as _mem_exc:
                        logger.warning(
                            "[plan-exec] Failed to extract tool calls from memory: %s", _mem_exc
                        )
                    # Any tool_use without a paired tool_result (interrupted mid-call)
                    for _tid, _rec in list(_pending_log.items()):
                        log_writer.schedule_tool_call_write(
                            {
                                "user_id": user_id or None,
                                "chat_id": chat_id or None,
                                "tool_name": _rec.get("tool_name", "unknown"),
                                "tool_call_id": _tid,
                                "tool_args": _rec.get("tool_args"),
                                "status": "failed",
                                "error_message": "no tool_result received",
                                "source": "subagent",
                                "subagent_log_id": _step_subagent_log_id,
                            }
                        )
                    _pending_log.clear()

                    # Extract text from reply
                    if hasattr(reply, "content"):
                        if isinstance(reply.content, str):
                            step_text = reply.content
                        elif isinstance(reply.content, list):
                            parts = []
                            for block in reply.content:
                                if hasattr(block, "text"):
                                    parts.append(block.text)
                                elif isinstance(block, dict) and "text" in block:
                                    parts.append(block["text"])
                                elif isinstance(block, str):
                                    parts.append(block)
                            step_text = "\n".join(parts)
                        else:
                            step_text = str(reply.content)
                    else:
                        step_text = str(reply)

                    # Strip <think>...</think> tags
                    import re

                    step_text = re.sub(
                        r"<think>.*?</think>", "", step_text, flags=re.DOTALL
                    ).strip()

                    from core.ontology.validator import requires_output_review

                    if step_text and requires_output_review(step_ontology_runtime):
                        from orchestration.subagents.ontology_reviewer import (
                            review_ontology_output,
                        )

                        yield {
                            "type": "ontology_review",
                            "status": "started",
                            "level": step_ontology_runtime.get(
                                "review_level", "checkpoint"
                            ),
                            "step_id": step.step_id,
                        }
                        trace = [
                            {
                                "type": "tool_result",
                                "tool_name": item.get("tool_name"),
                                "tool_id": item.get("tool_id"),
                                "result": item.get("result"),
                            }
                            for item in step_tool_calls
                            if "result" in item
                        ]
                        ontology_review = await review_ontology_output(
                            task=f"{plan.task_input}\n{step_instruction}",
                            answer=step_text,
                            runtime=step_ontology_runtime,
                            trace=trace,
                            citations=[],
                            user_id=user_id,
                            chat_id=chat_id,
                            model_name=model_name,
                        )
                        step_text = ontology_review["answer"]
                        yield {
                            "type": "ontology_review",
                            "status": "completed",
                            "level": step_ontology_runtime.get(
                                "review_level", "checkpoint"
                            ),
                            "verdict": ontology_review["verdict"],
                            "step_id": step.step_id,
                        }

                    yield {
                        "type": "plan_step_progress",
                        "step_id": step.step_id,
                        "delta": step_text,
                    }
                except asyncio.TimeoutError:
                    step_text = "步骤执行被取消"
                    logger.warning("[plan-exec] Step %d cancelled", step.step_order)
                except Exception as _reply_exc:
                    # Include exception class so empty-message errors (e.g. CancelledError)
                    # are still distinguishable; log full traceback for diagnosis.
                    _err_repr = f"{type(_reply_exc).__name__}: {_reply_exc}".strip(": ")
                    step_text = f"执行出错: {_err_repr or type(_reply_exc).__name__}"
                    logger.warning(
                        "[plan-exec] Step %d reply error (%s)",
                        step.step_order,
                        _err_repr,
                        exc_info=True,
                    )

                # Keep MCP clients alive to prevent GC cancel scope crashes.
                # They'll be terminated after all steps complete.
                _all_mcp_clients.extend(mcp_clients)

                logger.warning(
                    "[plan-exec] Step %d done, text len=%d", step.step_order, len(step_text)
                )

                # Track last step's output for use as final result
                if step_text:
                    last_step_text = step_text

                summary = _extract_summary(step_text, max_len=200)
                step_summaries.append(f"步骤{step.step_order}({step.title}): {summary}")

                if step_text:
                    trunc = step_text
                    if len(trunc) > _PER_STEP_OUTPUT_CHARS:
                        trunc = trunc[:_PER_STEP_OUTPUT_CHARS] + "\n…（输出过长已截断）"
                    step_outputs_for_context.append(
                        f"### 步骤{step.step_order}：{step.title}\n{trunc}"
                    )
                    if len(step_outputs_for_context) > _MAX_PREVIOUS_OUTPUTS:
                        step_outputs_for_context = step_outputs_for_context[-_MAX_PREVIOUS_OUTPUTS:]

                svc.update_step(
                    step.step_id,
                    status="success",
                    result_summary=summary,
                    ai_output=step_text[:5000],
                    tool_calls_log=step_tool_calls,
                    completed_at=datetime.utcnow(),
                )
                completed_count += 1

                await log_writer.finish_subagent_log(
                    _step_subagent_log_id,
                    status=_step_outcome,
                    output_content=step_text,
                    intermediate_steps=step_tool_calls[:100] if step_tool_calls else None,
                    tool_calls_count=len(step_tool_calls),
                    duration_ms=int((_time.monotonic() - _step_start_monotonic) * 1000),
                    error_message=_step_error_msg,
                )
                _plan_tool_count += len(step_tool_calls)

                yield {
                    "type": "plan_step_complete",
                    "step_id": step.step_id,
                    "status": "success",
                    "summary": summary,
                }

            except Exception as step_exc:
                logger.exception("Step %s failed", step.step_id)
                error_msg = str(step_exc)
                svc.update_step(
                    step.step_id,
                    status="failed",
                    error_message=error_msg,
                    completed_at=datetime.utcnow(),
                )
                await log_writer.finish_subagent_log(
                    _step_subagent_log_id,
                    status="failed",
                    error_message=error_msg,
                    output_content=step_text,
                    tool_calls_count=len(step_tool_calls),
                    duration_ms=int((_time.monotonic() - _step_start_monotonic) * 1000),
                )
                yield {
                    "type": "plan_step_complete",
                    "step_id": step.step_id,
                    "status": "failed",
                    "summary": f"执行失败: {error_msg}",
                }
                yield {
                    "type": "plan_error",
                    "plan_id": plan_id,
                    "step_id": step.step_id,
                    "error": error_msg,
                }
                # Continue to next step rather than aborting the entire plan
                step_summaries.append(f"步骤{step.step_order}({step.title}): 失败 - {error_msg}")

        # Plan complete
        logger.warning(
            "[plan-exec] === All steps done. completed=%d/%d ===", completed_count, plan.total_steps
        )
        final_status = "completed" if completed_count == plan.total_steps else "failed"
        if cancelled:
            final_status = "cancelled"

        overall_summary = f"共 {plan.total_steps} 个步骤，完成 {completed_count} 个"

        # ── Final result: use the last step's full output directly ──
        result_text = last_step_text

        svc.update_plan(
            plan_id,
            status=final_status,
            completed_steps=completed_count,
            result_summary=result_text[:2000] if result_text else overall_summary,
        )

        # Token usage: under 2.0 plan steps run agent.reply() directly, so per-step usage
        # is not aggregated here for now (the old 1.x _UsageTrackingModel proxy was removed).
        # For accurate billing, later subscribe to each step's reply_stream
        # ModelCallEndEvent. Report zero for now.
        exec_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "llm_call_count": 0,
        }

        yield {
            "type": "plan_complete",
            "plan_id": plan_id,
            "status": final_status,
            "summary": overall_summary,
            "result_text": result_text,
            "completed_steps": completed_count,
            "total_steps": plan.total_steps,
            "usage": exec_usage,
        }

        await log_writer.finish_subagent_log(
            _plan_subagent_log_id,
            status="success" if final_status == "completed" else final_status,
            output_content=result_text or overall_summary,
            intermediate_steps=step_summaries,
            token_usage=exec_usage,
            tool_calls_count=_plan_tool_count,
            duration_ms=int((_time.monotonic() - _plan_run_start) * 1000),
        )

    except Exception as exc:
        logger.exception("Plan execution failed")
        svc.update_plan(plan_id, status="failed", result_summary=str(exc))
        await log_writer.finish_subagent_log(
            _plan_subagent_log_id,
            status="failed",
            error_message=str(exc),
            duration_ms=int((_time.monotonic() - _plan_run_start) * 1000),
        )
        yield {"type": "plan_error", "plan_id": plan_id, "error": str(exc)}
    finally:
        # Now that SSE stream is done, safely terminate all MCP processes.
        _terminate_mcp_processes(_all_mcp_clients)
        _all_mcp_clients.clear()
        try:
            _log_ctx.__exit__(None, None, None)
        except Exception:
            pass


def _terminate_mcp_processes(mcp_clients: list) -> None:
    """Kill MCP subprocess PIDs and neutralize async cleanup references.

    The MCP StdIOStatefulClient uses an AsyncExitStack that holds async
    generators (stdio_client) with anyio cancel scopes.  If these objects
    are garbage-collected from a different asyncio Task than the one that
    created them, anyio raises 'Attempted to exit cancel scope in a
    different task' which crashes the ASGI SSE response.

    Fix: terminate the subprocess, then clear the AsyncExitStack's internal
    callback deque so GC won't trigger the async generator cleanup.
    """
    for client in mcp_clients:
        try:
            proc = getattr(client, "_process", None) or getattr(client, "process", None)
            if proc is not None and getattr(proc, "returncode", None) is None:
                proc.terminate()
        except Exception:
            pass
        # Neutralize ALL async references to prevent GC-triggered cancel
        # scope crash.  The client holds:
        #   client.client  → stdio_client async generator (has cancel scope)
        #   client.stack   → AsyncExitStack (holds generator's __aexit__)
        #   client.session → ClientSession
        try:
            stack = getattr(client, "stack", None)
            if stack is not None and hasattr(stack, "_exit_callbacks"):
                stack._exit_callbacks.clear()
            client.stack = None
            client.session = None
            client.client = None  # the async generator itself
            client.is_connected = False
        except Exception:
            pass


_HERE_MARKER_RE = re.compile(r"\{HERE:([^}]*)\}")


def _inject_here_marker(line: str, current_step_id: str) -> str:
    def _sub(m: "re.Match[str]") -> str:
        return " ← 你正在执行此步" if m.group(1) == current_step_id else ""

    return _HERE_MARKER_RE.sub(_sub, line)


def _build_plan_header_lines(plan: Optional[Any]) -> Tuple[List[str], List[str]]:
    """Pre-build the parts that don't change between steps.

    Returns (goal_lines, step_roadmap_lines). The roadmap lines have a
    ``{HERE}`` placeholder at the position of each step — the caller swaps
    in "← 你正在执行此步" (you are executing this step) for the current step
    and "" for the others.
    """
    goal_lines: List[str] = []
    roadmap_lines: List[str] = []
    if plan is None:
        return goal_lines, roadmap_lines

    plan_title = getattr(plan, "title", "") or ""
    plan_desc = getattr(plan, "description", "") or ""
    if plan_title or plan_desc:
        goal_lines.append("## 整体计划目标")
        if plan_title:
            goal_lines.append(f"**{plan_title}**")
        if plan_desc:
            goal_lines.append(plan_desc)
        goal_lines.append("")

    steps_list = list(getattr(plan, "steps", []) or [])
    if steps_list:
        roadmap_lines.append("## 完整步骤路线图")
        roadmap_lines.append("（仅作上下文，不要替别的步骤干活）")
        for s in steps_list:
            order = getattr(s, "step_order", "?")
            title = getattr(s, "title", "") or ""
            desc = getattr(s, "description", "") or ""
            if len(desc) > 200:
                desc = desc[:200] + "…"
            step_id = getattr(s, "step_id", "")
            line = f"- 步骤{order}：**{title}**{{HERE:{step_id}}}"
            if desc:
                line += f"\n  {desc}"
            roadmap_lines.append(line)
        roadmap_lines.append("")

    return goal_lines, roadmap_lines


def _build_step_instruction(
    step,
    previous_summaries: List[str],
    goal_lines: Optional[List[str]] = None,
    roadmap_lines: Optional[List[str]] = None,
    previous_step_outputs: Optional[List[str]] = None,
) -> str:
    """Assemble per-step instruction: pre-built goal + roadmap (with current
    step marker injected), previous outputs, and the current step's brief.
    """
    parts: List[str] = []
    if goal_lines:
        parts.extend(goal_lines)

    if roadmap_lines:
        current_id = getattr(step, "step_id", "")
        for raw in roadmap_lines:
            parts.append(_inject_here_marker(raw, current_id))

    if previous_step_outputs:
        parts.append("## 前序步骤的实际产出")
        for out in previous_step_outputs:
            parts.append(out)
            parts.append("")
    elif previous_summaries:
        parts.append("## 前序步骤结果摘要")
        for s in previous_summaries:
            parts.append(f"- {s}")
        parts.append("")

    parts.append("## 你现在要做的")
    parts.append(f"**{step.title}**")
    parts.append(step.description or "请完成上述任务。")
    parts.append(
        "\n## 执行要求\n"
        "- 严格聚焦"
        "「你正在执行此步」"
        "标注的步骤，**不要做后续步骤的工作**——后续步骤会有自己的 agent 接手。\n"
        "- 如果前序步骤已经产出了你需要的东西（例如文件、数据），**直接引用 / 复用**，不要重做。\n"
        "- 每个工具最多调用一次，避免重复调用同一工具。\n"
        "- 如果工具返回的信息已经足够，立即总结结果，不要继续搜索。\n"
        "- 完成后请用1-2句话总结执行结果。"
    )

    return "\n".join(parts)


def _extract_summary(text: str, max_len: int = 200) -> str:
    """Extract a short summary from step output text."""
    if not text:
        return "已完成"
    # Take the last paragraph or first sentence as summary
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    if not lines:
        return "已完成"
    # Use the last non-empty line as it's usually the conclusion
    summary = lines[-1]
    if len(summary) > max_len:
        summary = summary[:max_len] + "..."
    return summary
