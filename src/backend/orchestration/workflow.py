"""Minimal multi-agent workflow orchestration (AgentScope backend)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from core.llm.agent_factory import create_agent_executor
from core.llm.context_manager import (
    ContextBudget,
    ContextWindowManager,
    resolve_model_context_window,
)
from core.llm.message_compat import session_to_msgs, strip_thinking
from core.llm.mcp_manager import close_clients
from core.config.catalog_resolver import (
    enabled_skill_ids_from_context,
    enabled_mcp_ids_from_context,
    enabled_kb_ids_from_context,
)
from orchestration.streaming import StreamingAgent
from orchestration.citations import extract_citations_with_offset
from core.config.display_names import TOOL_DISPLAY_NAMES


# Project mode: extracted from chats.py's ctx and passed through to agent_factory so the system prompt renders the project section.
_PROJECT_CTX_KEYS = (
    "project_id",
    "project_name",
    "project_instructions",
    "project_folder_name",
    "project_folder_kind",
    "project_folder_id",
    "project_team_id",  # only set for team kind; passed by agent_factory to the MySpace tools for the TeamFolder path
    "project_files",
)


def _extract_project_ctx(context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract project-related fields from the workflow context. Returns None when there is no project_id."""
    if not context.get("project_id"):
        return None
    return {k: context.get(k) for k in _PROJECT_CTX_KEYS}


def _extract_skill_id_from_path(path: str) -> str:
    """Extract skill_id from a SKILL.md path (convention: .../skills/<skill_id>/SKILL.md)."""
    if not path:
        return ""
    import os

    norm = path.replace("\\", "/").strip().strip('"').strip("'")
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return ""
    if parts[-1].upper() == "SKILL.MD" and len(parts) >= 2:
        return parts[-2]
    base = os.path.basename(norm)
    if base.upper() == "SKILL.MD" and len(parts) >= 2:
        return parts[-2]
    return ""


# SSE tool-result payload builders (moved to routing.tool_payloads)
from orchestration.tool_payloads import (  # noqa: E402
    _build_read_artifact_payload,
    _build_read_tool_payload,
    _build_skill_load_payload,
    _build_view_text_file_payload,
    _tool_args_ready,
    _FAST_EMIT_TOOLS,
)


# Process-level persistent references: after each streaming run,
# (streaming_agent, mcp_clients) is pushed in so HTTP transport MCP clients
# (currently retrieve_dataset_content, a streamable_http client) are never GC'd.
#
# Cause: the HTTP client uses an anyio TaskGroup + CancelScope. For stdio
# clients, streaming_agent.shutdown() SIGTERMs the subprocess directly and
# cleans up fine; HTTP clients (`_process is None`) are skipped by shutdown and
# eventually land in Python GC running __aexit__ — that __aexit__ almost
# certainly runs on the wrong task, triggering
#   RuntimeError: Attempted to exit cancel scope in a different task...
# The cancel signal flows back through the event loop and takes out the current
# stream's agent.reply() / the next item's create_subprocess_exec along with
# it. Reproduced in both production and local.
#
# Same root cause and same fix as batch_orchestrator._persistent_clients:
# accept a small memory leak (one leftover HTTP keepalive socket per
# conversation) in exchange for a runner that never deadlocks. Released
# together when the worker process exits/restarts.
_persistent_clients: list = []


# Re-export public helpers for backward compatibility
from orchestration.message_parser import (  # noqa: F401
    looks_markdown as _looks_markdown,
    resolve_sources_conflict as _resolve_sources_conflict,
)
from orchestration.memory_integration import (  # noqa: F401
    launch_memory_retrieval,
    build_frozen_memory_block,
    build_user_identity_block,
    inject_frozen_memory,
    save_memories_background,
)

logger = logging.getLogger(__name__)


_BATCH_RUNNER_ID = "batch_runner"


def _resolve_batch_runner_visibility(
    context: Dict[str, Any],
    enabled_mcp_ids: Optional[List[str]],
) -> Optional[List[str]]:
    """Decide whether ``batch_runner`` should be in the effective MCP set.

    - ``batch_chat`` (App Center batch-execution entry): force-include, even if the
      user's catalog config doesn't list it. Wins over the unattended hide.
    - ``plan_chat`` / ``automation_run`` / ``disable_batch_plan``: hide,
      because the batch_plan flow requires a confirmation dialog and these
      runs have no UI to confirm with.
    - Otherwise: pass through unchanged.

    When *enabled_mcp_ids* is ``None`` we resolve the catalog default once.
    """
    if context.get("batch_chat"):
        if enabled_mcp_ids is None:
            from core.config.catalog import get_enabled_ids

            enabled_mcp_ids = list(get_enabled_ids("mcp"))
        if _BATCH_RUNNER_ID not in enabled_mcp_ids:
            return [*enabled_mcp_ids, _BATCH_RUNNER_ID]
        return enabled_mcp_ids

    if not (
        context.get("plan_chat")
        or context.get("automation_run")
        or context.get("disable_batch_plan")
    ):
        return enabled_mcp_ids

    if enabled_mcp_ids is None:
        from core.config.catalog import get_enabled_ids

        enabled_mcp_ids = list(get_enabled_ids("mcp"))
    return [m for m in enabled_mcp_ids if m != _BATCH_RUNNER_ID]


def _build_skill_injection(context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build an explicit-invocation hint message (skill load instructions + MCP tool activation notice).

    Trigger sources:
    - ``skill_id`` (single skill, slash-command selection) / ``skill_ids``
      (skill list, expanded from an explicitly referenced plugin)
      → inject each skill's SKILL.md load instruction.
    - ``mcp_ids`` (MCP servers activated together when a plugin is explicitly
      referenced) → these tools are already force-enabled into the toolset
      (see _build_ctx); here we append a sentence telling the model they are
      ready and can be called on demand.

    Aligned with Claude Code / Codex: once enabled, MCP tools stay resident in
    the toolset and the model calls them by description on its own — no
    per-tool pinning; explicit invocation only ensures "skill instructions are
    present + plugin tools are activated with a hint to prefer them".

    Returns a dict {"role": "user", "content": "..."} or None.
    """
    skill_ids: List[str] = []
    single = context.get("skill_id")
    if single:
        skill_ids.append(str(single))
    multi = context.get("skill_ids")
    if isinstance(multi, list):
        skill_ids.extend(str(x) for x in multi if x)
    seen: set = set()
    skill_ids = [i for i in skill_ids if not (i in seen or seen.add(i))]

    mcp_ids = [str(m) for m in (context.get("mcp_ids") or []) if m]
    plugin_name = context.get("plugin_name")

    if not skill_ids and not mcp_ids:
        return None

    sections: List[str] = []

    # ── Skills: load instructions ──
    if skill_ids:
        try:
            from core.agent_skills.loader import get_skill_loader

            loader = get_skill_loader()
            metadata_all = loader.load_all_metadata()
            entries: List[str] = []
            for sid in skill_ids:
                meta = metadata_all.get(sid)
                if not meta:
                    logger.warning("[skill_inject] skill_id=%s not found", sid)
                    continue
                # Trigger materialization (DB skill written to disk →
                # bind-mounted/pushed into the sandbox), but the injected
                # prompt must use the **sandbox path** /workspace/skills/<id>,
                # not the backend materialized path returned by get_skill_dir
                # (/app/storage/sandbox_skills/<id>) — the backend path does
                # not exist inside the sandbox, and if the model uses it with
                # bash (cat/ls/python) it gets No such file or directory.
                # Same scheme as agent_factory._SKILL_INSTRUCTION_TEMPLATE:
                # the basename is the skill id; on view_text_file reads,
                # skill_tool._resolve_skill_path maps it back to the backend file.
                skill_dir = loader.get_skill_dir(sid)
                if not skill_dir:
                    logger.warning("[skill_inject] skill_id=%s has no skill dir", sid)
                    continue
                sandbox_dir = f"/workspace/skills/{skill_dir.rstrip('/').split('/')[-1]}"
                entries.append(
                    f'- 「{meta.name}」：view_text_file(file_path="{sandbox_dir}/SKILL.md")'
                )
            if entries:
                sections.append(
                    "技能（必须先加载文件再执行，不要跳过直接调用 bash 或其它工具）：\n"
                    + "\n".join(entries)
                )
        except Exception as e:  # noqa: BLE001
            logger.error("[skill_inject] failed to load skills %s: %s", skill_ids, e)

    # ── MCP: activation notice (the tools themselves are already in the toolset; call by description) ──
    if mcp_ids:
        sections.append(
            f"MCP 工具：本插件包含的 {len(mcp_ids)} 个 MCP 工具服务已激活并就绪，"
            "可直接按需调用（无需加载文件），请优先使用它们完成相关任务。"
        )

    if not sections:
        return None

    header = (
        f"用户已显式调用插件「{plugin_name}」，请优先采用其能力："
        if plugin_name
        else "用户已显式指定使用以下能力，请优先采用："
    )
    return {
        "role": "user",
        "content": (
            "<explicit_invocation>\n"
            + header
            + "\n"
            + "\n\n".join(sections)
            + "\n</explicit_invocation>"
        ),
    }


def _parse_agent_mentions(message: str, available_agents: list) -> list:
    """Parse @agent_name mentions from user message.

    Returns list of matched ``agent_id``s in the order they appear in
    *message*.

    When several agent names share a prefix (e.g. ``搜索`` vs ``搜索助手``)
    a naive ``"@搜索" in message`` check matches "@搜索" *inside*
    "@搜索助手", so typing ``@搜索助手`` would falsely also mention the
    shorter ``搜索`` agent — and the prompt hint built downstream would
    instruct the LLM to call both. We sort candidates by name length
    descending and reserve consumed character ranges so the longest
    matching name wins and prefix-shadow matches are skipped.
    """
    if not available_agents:
        return []

    candidates = [a for a in available_agents if a.get("name")]
    if not candidates:
        return []
    candidates.sort(key=lambda a: len(a["name"]), reverse=True)

    consumed: list = []  # non-overlapping (start, end) char ranges

    def _overlaps(start: int, end: int) -> bool:
        for s, e in consumed:
            if start < e and end > s:
                return True
        return False

    # (position, agent_id) — sorted at the end to preserve message order
    hits: list = []
    for agent in candidates:
        token = "@" + agent["name"]
        start = 0
        while True:
            idx = message.find(token, start)
            if idx < 0:
                break
            end = idx + len(token)
            if not _overlaps(idx, end):
                consumed.append((idx, end))
                hits.append((idx, agent["agent_id"]))
            start = idx + len(token)

    hits.sort(key=lambda x: x[0])
    seen: set = set()
    ordered: list = []
    for _, aid in hits:
        if aid not in seen:
            seen.add(aid)
            ordered.append(aid)
    return ordered


# ------------------------------------------------------------------
# Data containers
# ------------------------------------------------------------------


@dataclass
class WorkflowResult:
    route: str = "main"
    response: str = ""
    is_markdown: bool = False
    sources: List[Dict[str, Any]] = field(default_factory=list)
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------
# Synchronous workflow (non-streaming)
# ------------------------------------------------------------------


def run_chat_workflow(
    *,
    session_messages: List[Dict[str, Any]],
    user_message: str,
    context: Dict[str, Any],
) -> WorkflowResult:
    """Run route -> target execution."""

    # ── Inject explicit skill instructions ──
    skill_msg = _build_skill_injection(context)
    if skill_msg:
        session_messages.insert(-1, skill_msg)
        logger.info("[skill_inject] injected skill instructions for '%s'", context.get("skill_id"))

    warnings: List[str] = []
    enabled_skill_ids = enabled_skill_ids_from_context(context)
    enabled_kb_ids = enabled_kb_ids_from_context(context)
    enabled_mcp_ids = enabled_mcp_ids_from_context(context)

    enabled_mcp_ids = _resolve_batch_runner_visibility(context, enabled_mcp_ids)

    _workflow_user_id = str(context.get("user_id", ""))
    _workflow_model_name = str(context.get("model_name", ""))
    _workflow_model_provider_id = str(context.get("model_provider_id", "") or "")
    _workflow_chat_mode = str(context.get("chat_mode", "") or "")
    _workflow_mem_enabled = bool(context.get("memory_enabled", False))
    _reranker_enabled = bool(context.get("reranker_enabled", False))

    _workflow_batch_chat = bool(context.get("batch_chat", False))

    async def _run():
        agent, mcp_clients = await create_agent_executor(
            agent_spec=None,
            enabled_skill_ids=enabled_skill_ids,
            enabled_mcp_ids=enabled_mcp_ids,
            enabled_kb_ids=enabled_kb_ids,
            current_user_id=_workflow_user_id,
            reranker_enabled=_reranker_enabled,
            model_name=_workflow_model_name,
            model_provider_id=_workflow_model_provider_id,
            chat_mode=_workflow_chat_mode,
            memory_enabled=_workflow_mem_enabled,
            batch_mode=_workflow_batch_chat,
            project_ctx=_extract_project_ctx(context),
            channel_origin=context.get("channel_origin"),
            automation_run=bool(context.get("automation_run")),
        )
        try:
            from agentscope.message import Msg, TextBlock

            # AgentScope 2.0: ctx → agent.state (AgentRuntimeState), replacing _jx_context.
            # Uploaded/history files are written into state by
            # apply_request_context, then injected uniformly at reply time by
            # FileContextMiddleware (not hand-copied here).
            try:
                agent.state.apply_request_context(context, user_message or "")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[workflow] set agent.state failed: %s", exc)

            # PreTurn compaction safety net (symmetric with the streaming path
            # — both workflow entry points protect themselves, and future new
            # callers get it automatically). Zero overhead below the threshold.
            _pt_messages = session_messages
            try:
                from core.services.compaction_service import maybe_run_pre_turn_compaction

                _actual_model = getattr(agent.model, "model_name", _workflow_model_name)
                _pt_messages, _ = await maybe_run_pre_turn_compaction(
                    context.get("chat_id"), session_messages, model_name=_actual_model
                )
            except Exception as _pt_exc:  # noqa: BLE001
                logger.warning("[workflow] pre-turn compaction failed: %s", _pt_exc)

            # Load history EXCLUDING the last user message — reply() adds it.
            history = list(_pt_messages)
            if history and history[-1].get("role") in ("user", "human"):
                history.pop()

            _ctx_mgr = ContextWindowManager.for_model(_workflow_model_name)
            history = _ctx_mgr.trim_history(history)

            if history:
                agent.state.context.extend(session_to_msgs(history))

            # Uploaded-file context is injected uniformly at reply time by
            # FileContextMiddleware (apply_request_context already wrote
            # uploaded_files into state; the middleware validates attachment
            # ownership by state.user_id); no manual append here, otherwise it
            # would duplicate the middleware's injection and waste tokens.

            user_msg = Msg(
                name="user", role="user", content=[TextBlock(type="text", text=user_message or "")]
            )
            result = await agent.reply(inputs=user_msg)
            return strip_thinking(result.get_text_content() or "")
        finally:
            await close_clients(mcp_clients)

    try:
        import asyncio as _asyncio

        response = _asyncio.run(_run())
    except Exception as e:
        warnings.append(f"Agent execution error: {str(e)[:200]}")
        response = ""

    return WorkflowResult(
        route="main",
        response=response,
        is_markdown=_looks_markdown(response),
        sources=_resolve_sources_conflict([]),
        artifacts=[],
        warnings=warnings,
        meta={},
    )


# ------------------------------------------------------------------
# Sub-agent direct conversation
# ------------------------------------------------------------------


async def _astream_subagent_direct(
    *,
    agent_id: str,
    session_messages: List[Dict[str, Any]],
    user_message: str,
    context: Dict[str, Any],
) -> AsyncIterator[Dict[str, Any]]:
    """Stream a direct conversation with a user-created sub-agent.

    Loads the UserAgent config from DB and uses it to build the agent
    with custom system_prompt, MCP tools, skills, KB, and model params.
    Shares the same streaming/memory/citation infrastructure as the main route.
    """
    import time as _time

    _wf_start = _time.monotonic()

    # Load UserAgent ORM object
    from core.db.engine import SessionLocal
    from core.services.user_agent_service import UserAgentService

    with SessionLocal() as _db:
        svc = UserAgentService(_db)
        user_agent = svc.get_raw_by_id(
            agent_id,
            user_id=str(context.get("user_id", "")),
        )
        # Eagerly load fields before session closes
        _ = user_agent.mcp_server_ids, user_agent.skill_ids, user_agent.kb_ids
        _ = user_agent.system_prompt, user_agent.model_provider_id
        _ = user_agent.max_iters, user_agent.temperature, user_agent.max_tokens, user_agent.timeout

    # ── [memory] Non-blocking retrieval: background task, skipped on budget timeout ───
    _mem0_user_id = str(context.get("user_id", ""))
    _mem0_workspace_id = str(context.get("workspace_id", "") or "default")
    _mem0_chat_id = context.get("chat_id") or context.get("conversation_id")
    _mem0_enabled = bool(context.get("memory_enabled", False))
    _mem0_write_enabled = bool(context.get("memory_write_enabled", False))
    # Under a team project chats.py passes "team:<tid>"; personal/default spaces don't pass it and fall back to the real user_id
    _mem0_scope_user_id = str(context.get("memory_scope_user_id", "") or _mem0_user_id)
    logger.info(
        "[subagent] user=%s scope=%s ws=%s agent=%s enabled=%s",
        _mem0_user_id,
        _mem0_scope_user_id,
        _mem0_workspace_id,
        agent_id,
        _mem0_enabled,
    )

    _memory_task = await launch_memory_retrieval(
        _mem0_scope_user_id,
        user_message,
        _mem0_enabled,
        workspace_id=_mem0_workspace_id,
    )

    warnings: List[str] = []
    full_response = ""
    displayed_tools: set[str] = set()
    all_citations: List[Dict[str, Any]] = []
    citation_offsets: Dict[str, int] = {}

    try:
        yield {"type": "thinking", "message": "正在连接子智能体..."}

        _stream_user_id = str(context.get("user_id", ""))
        _stream_model_name = str(context.get("model_name", ""))
        _stream_model_provider_id = str(context.get("model_provider_id", "") or "")
        _stream_chat_mode = str(context.get("chat_mode", "") or "")
        _stream_reranker = bool(context.get("reranker_enabled", False))

        # Create agent with sub-agent config overrides
        agent, mcp_clients = await create_agent_executor(
            agent_spec=None,
            enabled_mcp_ids=None,  # overridden by user_agent inside factory
            enabled_skill_ids=None,  # overridden by user_agent inside factory
            enabled_kb_ids=None,  # overridden by user_agent inside factory
            current_user_id=_stream_user_id,
            reranker_enabled=_stream_reranker,
            model_name=_stream_model_name,
            model_provider_id=_stream_model_provider_id,
            chat_mode=_stream_chat_mode,
            memory_enabled=_mem0_enabled,
            user_agent=user_agent,
            # Same as the main agent: pass chat_id → the sandbox session uses
            # the chat_id-keyed "user-bound persistent sandbox" (mounting the
            # per-user credential volumes for lark/dws/email etc.). Omitting it
            # falls back to an ephemeral light sandbox (no credentials) — the
            # root cause of Feishu/DingTalk CLIs reporting "not configured" in
            # direct sub-agent conversations.
            chat_id=context.get("chat_id"),
            project_ctx=_extract_project_ctx(context),
            channel_origin=context.get("channel_origin"),
            automation_run=bool(context.get("automation_run")),
        )

        logger.info("[subagent] agent created in %.0fms", (_time.monotonic() - _wf_start) * 1000)

        # ── Frozen-block injection: user identity (always injected) + memory snapshot (loaded only when persistent memory is on) ───
        _identity_block = await build_user_identity_block(_mem0_user_id)
        frozen_block = ""
        if _mem0_enabled:
            frozen_block = await build_frozen_memory_block(
                _mem0_scope_user_id,
                _mem0_workspace_id,
                _memory_task,
                memory_enabled=_mem0_enabled,
            )
        else:
            logger.debug(
                "[subagent] memory load skipped: memory_enabled=False (user=%s)", _mem0_user_id
            )
        if frozen_block or _identity_block:
            session_messages = await inject_frozen_memory(
                frozen_block,
                session_messages,
                identity_block=_identity_block,
            )

        # ── Context window management ─────────────────────────────
        # A sub-agent's context is shared from the main agent (it has no
        # checkpoint system of its own); over budget it is trimmed directly to
        # the token budget (layer-C compression of oversized user messages
        # still happens inside manage_context).
        _actual_model = getattr(agent.model, "model_name", _stream_model_name)
        ctx_manager = ContextWindowManager.for_model(_actual_model)
        trimmed, dropped_messages = ctx_manager.manage_context(session_messages)
        if dropped_messages:
            logger.warning(
                "[subagent] context over budget: dropped %d message(s)", len(dropped_messages)
            )
        session_messages = trimmed

        streaming_agent = StreamingAgent(agent, mcp_clients)
        skill_load_ids: set = set()
        # tool_id → skill_id (parsed from the tool_call's file_path; looked up at tool_result time to replace the SSE payload with the curated detail, avoiding sending the full SKILL.md text to the frontend)
        skill_id_by_tool_id: Dict[str, str] = {}
        # tool_id → tool_args, used at the tool_result stage to recover view_text_file's file_path/ranges
        view_text_file_args: Dict[str, Dict[str, Any]] = {}

        # Project scope is no longer passed via ContextVar — it now travels as
        # explicit parameters along the call chain (agent_factory closes
        # ProjectScope into every register_* tool; chats.py's finishing
        # _persist_artifacts reconstructs the scope from the workflow context
        # and passes it explicitly). See the header comment in
        # core/services/project_scope.py.

        try:
            async for event_type, payload in streaming_agent.stream(session_messages, context):
                if event_type == "text_delta":
                    full_response += payload
                    yield {"type": "content", "event": "ai_message", "delta": payload}

                elif event_type == "thinking_delta":
                    yield {"type": "thinking", "delta": payload}

                elif event_type == "tool_call":
                    tool_name = payload.get("name", "unknown")
                    tool_id = payload.get("id", "")
                    tool_args = payload.get("args", {})

                    _is_fast_emit = tool_name in _FAST_EMIT_TOOLS
                    if tool_id and tool_id in displayed_tools:
                        if _is_fast_emit and tool_args:
                            pass  # re-emit with updated args
                        else:
                            continue
                    if not _tool_args_ready(tool_name, tool_args):
                        continue
                    if tool_id:
                        displayed_tools.add(tool_id)

                    is_skill_load = (
                        tool_name == "view_text_file"
                        and isinstance(tool_args, dict)
                        and "SKILL.md" in str(tool_args.get("file_path", ""))
                    )
                    if is_skill_load and tool_id:
                        skill_load_ids.add(tool_id)
                        _sid = _extract_skill_id_from_path(str(tool_args.get("file_path", "")))
                        if _sid:
                            skill_id_by_tool_id[tool_id] = _sid
                    # Non-skill view_text_file also needs trimming at the tool_result stage → record the args
                    if (
                        tool_name in ("view_text_file", "Read")
                        and not is_skill_load
                        and tool_id
                        and isinstance(tool_args, dict)
                    ):
                        view_text_file_args[tool_id] = tool_args
                    emit_name = "load_skill" if is_skill_load else tool_name
                    display_name = (
                        "加载技能"
                        if is_skill_load
                        else TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
                    )
                    safe_args = tool_args if isinstance(tool_args, dict) else {}

                    yield {
                        "type": "tool_call",
                        "tool_name": emit_name,
                        "tool_display_name": display_name,
                        "tool_args": safe_args,
                        "input": safe_args,
                        "tool_id": tool_id,
                    }

                elif event_type == "tool_result":
                    tool_name = payload.get("name", "unknown")
                    tool_id = payload.get("id", "")
                    is_skill_result = (tool_id and tool_id in skill_load_ids) or (
                        tool_name == "view_text_file"
                        and "SKILL.md" in str(payload.get("content", ""))
                    )
                    if is_skill_result:
                        tool_name = "load_skill"
                    tool_content = payload.get("content", "")

                    try:
                        tool_result_json = json.loads(tool_content) if tool_content else {}
                    except json.JSONDecodeError:
                        tool_result_json = {"result": tool_content}

                    # Skill load: replace the full SKILL.md text with the same
                    # curated detail used by the capability center; affects
                    # only the SSE payload sent to the frontend — the agent's
                    # own memory still holds the full content
                    if is_skill_result:
                        _sid = skill_id_by_tool_id.get(tool_id, "") or _extract_skill_id_from_path(
                            str(tool_content)
                        )
                        tool_result_json = _build_skill_load_payload(_sid)
                    elif tool_name == "view_text_file":
                        # Plain file read (AgentScope built-in view_text_file): replace with file metadata + short preview
                        tool_result_json = _build_view_text_file_payload(
                            view_text_file_args.get(tool_id, {}), tool_content
                        )
                    elif tool_name == "Read":
                        # Claude-Code-style Read tool: JSON payload, content holds the whole file
                        tool_result_json = _build_read_tool_payload(
                            view_text_file_args.get(tool_id, {}), tool_result_json
                        )
                    elif tool_name == "read_artifact":
                        tool_result_json = _build_read_artifact_payload(tool_result_json)

                    extracted_query = ""
                    if isinstance(tool_result_json, dict) and "result" in tool_result_json:
                        result_data = tool_result_json["result"]
                        if isinstance(result_data, dict):
                            extracted_query = result_data.get(
                                "query", result_data.get("question", "")
                            )

                    cit_items = extract_citations_with_offset(
                        tool_name, tool_id, tool_result_json, citation_offsets
                    )
                    cit_dicts = [c.to_dict() for c in cit_items]
                    all_citations.extend(cit_dicts)

                    yield {
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "tool_args": {"query": extracted_query} if extracted_query else {},
                        "result": tool_result_json,
                        "tool_id": tool_id,
                        "citations": cit_dicts,
                    }

                elif event_type == "heartbeat":
                    yield {"type": "heartbeat"}

                elif event_type == "tool_pending":
                    yield {"type": "tool_pending", **(payload or {})}

                elif event_type == "subagent_event":
                    # Bypass channel for the sub-agent's internal
                    # thinking/tool_call/tool_result/content — attached under
                    # the call_subagent tool card that launched it (linked via
                    # parent_tool_id).
                    yield {"type": "subagent_event", **(payload or {})}

                elif event_type in ("file_confirm", "design_pick"):
                    # Confirmation-type events (§13 MySpace write confirm /
                    # site-design pick-one-of-three): a tool coroutine has
                    # suspended waiting for the user's out-of-band action.
                    # Pass through to the frontend to show the confirmation
                    # card; the agent task stays blocked in that tool and this
                    # SSE stream does not end — after the out-of-band
                    # POST /file-confirm the tool resumes in place.
                    yield {"type": event_type, **(payload or {})}

                elif event_type == "error":
                    # payload may be a real exception object (kind=="err") or a
                    # dict (e.g. ExceedMaxIters mapped to {"kind":..,"name":..}).
                    # Raising the latter directly gives a TypeError, masking
                    # the real situation — wrap it in a RuntimeError.
                    if isinstance(payload, BaseException):
                        raise payload
                    raise RuntimeError(str(payload))

        finally:
            # shutdown() only SIGTERMs stdio subprocesses; HTTP clients
            # (_process is None) are skipped. Push (streaming_agent,
            # mcp_clients) into the process-level persistent list to prevent
            # HTTP clients being GC'd and triggering the anyio cross-task
            # cancel scope bug. See the _persistent_clients comment at the top
            # of the module.
            await streaming_agent.shutdown()
            _persistent_clients.append((streaming_agent, list(mcp_clients)))

    except Exception as e:
        import traceback

        logger.error("subagent_stream_error: %s\n%s", e, traceback.format_exc())
        warnings.append(f"Streaming error: {str(e)[:200]}")

        if displayed_tools and not full_response:
            fallback_msg = (
                "抱歉，我在整理工具调用的结果时遇到了问题。以上是已获取的工具执行结果，请参考。"
            )
            full_response = fallback_msg
            yield {"type": "content", "event": "ai_message", "delta": fallback_msg}
        elif not full_response:
            raise

    yield {
        "type": "meta",
        "route": f"subagent:{agent_id}",
        "is_markdown": _looks_markdown(full_response),
        "sources": _resolve_sources_conflict([]),
        "artifacts": [],
        "warnings": warnings,
        "citations": all_citations,
        "usage": streaming_agent.get_usage(),
    }

    # ── [memory] Post-response pipeline (SSE already closed, user isn't waiting) ────
    # No memory is written unless the user opted into memory_write_enabled (first gate).
    if _mem0_write_enabled:
        save_memories_background(
            _mem0_user_id,
            user_message,
            full_response,
            _mem0_write_enabled,
            workspace_id=_mem0_workspace_id,
            chat_id=_mem0_chat_id,
            scope_user_id=_mem0_scope_user_id,
        )
    else:
        logger.debug("[subagent] memory save skipped: write_enabled=False (user=%s)", _mem0_user_id)


# ------------------------------------------------------------------
# Streaming workflow
# ------------------------------------------------------------------


async def astream_chat_workflow(
    *,
    session_messages: List[Dict[str, Any]],
    user_message: str,
    context: Dict[str, Any],
):
    """Stream route -> handoff -> target execution.

    Yields chunks in the format:
    - {"type": "content", "delta": "text chunk"}
    - {"type": "tool_call", ...}
    - {"type": "tool_result", ...}
    - {"type": "meta", "route": "...", "sources": [...], ...}
    """

    # ── Sub-agent direct conversation mode ──
    _agent_id = context.get("agent_id")
    if _agent_id:
        async for chunk in _astream_subagent_direct(
            agent_id=_agent_id,
            session_messages=session_messages,
            user_message=user_message,
            context=context,
        ):
            yield chunk
        return

    # ── Inject explicit skill instructions ──
    skill_msg = _build_skill_injection(context)
    if skill_msg:
        session_messages.insert(-1, skill_msg)
        logger.info("[skill_inject] injected skill instructions for '%s'", context.get("skill_id"))

    # ── [memory] Retrieval launched as background task, NOT awaited here ──
    # New non-blocking path: launch_memory_retrieval() returns a Task
    # immediately; the actual result gets a short wait via
    # asyncio.wait_for(timeout=0.05) inside build_frozen_memory_block(); if the
    # budget is exceeded, Fact injection is skipped and only the L1 Profile is
    # used. Never blocks the SSE first frame.
    _mem0_user_id = str(context.get("user_id", ""))
    _mem0_workspace_id = str(context.get("workspace_id", "") or "default")
    _mem0_chat_id = context.get("chat_id") or context.get("conversation_id")
    _mem0_enabled = bool(context.get("memory_enabled", False))
    _mem0_write_enabled = bool(context.get("memory_write_enabled", False))
    # Under a team project chats.py passes "team:<tid>"; personal/default spaces don't pass it and fall back to the real user_id
    _mem0_scope_user_id = str(context.get("memory_scope_user_id", "") or _mem0_user_id)
    logger.info(
        "[memory] user=%s scope=%s ws=%s chat=%s enabled=%s write=%s",
        _mem0_user_id,
        _mem0_scope_user_id,
        _mem0_workspace_id,
        _mem0_chat_id,
        _mem0_enabled,
        _mem0_write_enabled,
    )

    _memory_task = await launch_memory_retrieval(
        _mem0_scope_user_id,
        user_message,
        _mem0_enabled,
        workspace_id=_mem0_workspace_id,
    )

    # ── Main-route streaming ──────────────────────────────────────
    warnings: List[str] = []
    full_response = ""
    displayed_tools: set[str] = set()
    all_citations: List[Dict[str, Any]] = []
    citation_offsets: Dict[str, int] = {}

    try:
        import time as _time

        _wf_start = _time.monotonic()

        yield {"type": "thinking", "message": "正在分析您的问题..."}

        _stream_user_id = str(context.get("user_id", ""))
        _stream_model_name = str(context.get("model_name", ""))
        _stream_reranker = bool(context.get("reranker_enabled", False))
        enabled_skill_ids = enabled_skill_ids_from_context(context)
        enabled_kb_ids = enabled_kb_ids_from_context(context)
        enabled_mcp_ids = enabled_mcp_ids_from_context(context)

        _stream_unattended = bool(
            context.get("plan_chat")
            or context.get("automation_run")
            or context.get("disable_batch_plan")
        )
        enabled_mcp_ids = _resolve_batch_runner_visibility(context, enabled_mcp_ids)

        # ── Load visible sub-agents for main agent routing ──
        _visible_subagents: list = []
        _mentioned_ids: list = []
        try:
            from core.db.engine import SessionLocal as _SessionLocal
            from core.services.user_agent_service import UserAgentService as _UAS

            with _SessionLocal() as _db:
                _ua_svc = _UAS(_db)
                _visible_subagents = _ua_svc.list_for_user(_stream_user_id)
            # Parse @mention in user message
            if _visible_subagents:
                _mentioned_ids = _parse_agent_mentions(user_message, _visible_subagents)
        except Exception as _exc:
            logger.warning("[workflow] failed to load visible subagents: %s", _exc)

        # Create agent (with model-aware CompressionConfig + optional native LTM)
        _plan_chat = bool(context.get("plan_chat", False))
        _batch_chat = bool(context.get("batch_chat", False))
        agent, mcp_clients = await create_agent_executor(
            agent_spec=None,
            enabled_skill_ids=enabled_skill_ids,
            enabled_mcp_ids=enabled_mcp_ids,
            enabled_kb_ids=enabled_kb_ids,
            current_user_id=_stream_user_id,
            reranker_enabled=_stream_reranker,
            model_name=_stream_model_name,
            memory_enabled=_mem0_enabled,
            visible_subagents=_visible_subagents if _visible_subagents else None,
            plan_mode=_plan_chat,
            batch_mode=_batch_chat,
            chat_id=context.get("chat_id"),
            project_ctx=_extract_project_ctx(context),
            channel_origin=context.get("channel_origin"),
            automation_run=bool(context.get("automation_run")),
            # The single positive source of truth for enter_plan_mode: enabled
            # only for "interactive main chats that can host plan mode" — has
            # a chat_id, not a channel bot, not automation, not plan_chat, not
            # batch. Channels/automation have no UI for the user to confirm a
            # plan; plan_chat/batch have their own orchestration and must not
            # nest plan mode.
            top_level_chat=(
                bool(context.get("chat_id"))
                and not context.get("channel_origin")
                and not bool(context.get("automation_run"))
                and not _plan_chat
                and not _batch_chat
            ),
        )

        logger.info("[workflow] agent created in %.0fms", (_time.monotonic() - _wf_start) * 1000)

        # ── Inject per-turn @-mention hint into the current user message ──
        # Keeps it OUT of the system prompt so the LLM provider's prefix cache
        # hits across turns within a chat (otherwise every turn with different
        # @mentions would re-build the cache from scratch).
        if _mentioned_ids and _visible_subagents:
            from core.llm.subagent_tool import build_subagent_mention_hint

            _mention_hint = build_subagent_mention_hint(_visible_subagents, _mentioned_ids)
            if (
                _mention_hint
                and session_messages
                and session_messages[-1].get("role") in ("user", "human")
            ):
                session_messages[-1] = {
                    **session_messages[-1],
                    "content": _mention_hint + "\n" + (session_messages[-1].get("content") or ""),
                }

        # ── PreTurn compaction safety net (aligned with Codex pre-turn compaction) ──
        # When end-of-turn background compaction failed/was skipped, or the
        # previous turn's tool calls blew up the history, compact once
        # synchronously with the same cross-turn compaction mechanism before
        # this turn starts and write a checkpoint. Below the threshold, only a
        # pure byte estimate is done (no DB / no LLM), so first-token latency
        # is unaffected. Must run **before** the frozen memory block injection
        # — identity/memory blocks are re-injected every turn and must not be
        # baked into the persistent checkpoint.
        # The model window is preferentially read straight off the model object
        # (make_chat_model bakes in the real context_size per the Config model
        # configuration at construction time, no default fallback), saving the
        # streaming path one synchronous DB query; only if missing on the
        # object do we fall back to resolve — unconfigured raises, fail loud,
        # never silently run with the wrong window.
        # preturn and manage_context below share the same value.
        _actual_model = getattr(agent.model, "model_name", _stream_model_name)
        _ctx_window = int(getattr(agent.model, "context_size", 0) or 0)
        if _ctx_window <= 0:
            _ctx_window = resolve_model_context_window(_actual_model)
        try:
            from core.services.compaction_service import maybe_run_pre_turn_compaction

            session_messages, _ = await maybe_run_pre_turn_compaction(
                context.get("chat_id"),
                session_messages,
                model_name=_actual_model,
                context_window=_ctx_window,
            )
        except Exception as _pt_exc:  # noqa: BLE001
            logger.warning("[workflow] pre-turn compaction failed: %s", _pt_exc)

        # ── Frozen-block injection: user identity (always injected) + memory snapshot (loaded only when persistent memory is on) ──
        # The L1 Profile always reads the DB (fast); L2 Facts are injected only
        # if memory_task has already completed — otherwise this turn's Facts
        # are dropped to protect first-frame latency.
        _identity_block = await build_user_identity_block(_mem0_user_id)
        frozen_block = ""
        if _mem0_enabled:
            frozen_block = await build_frozen_memory_block(
                _mem0_scope_user_id,
                _mem0_workspace_id,
                _memory_task,
                memory_enabled=_mem0_enabled,
            )
        else:
            logger.debug(
                "[workflow] memory load skipped: memory_enabled=False (user=%s)", _mem0_user_id
            )
        if frozen_block or _identity_block:
            session_messages = await inject_frozen_memory(
                frozen_block,
                session_messages,
                identity_block=_identity_block,
            )

        # ── Context window management (last line of defense) ────────────
        # PreTurn compaction already acted as the safety net before frozen
        # block injection; here we keep only manage_context's layer C
        # (compressing an oversized single user message) + token-budget
        # trimming. Normally the drop branch is never reached — if it is, we
        # only log (no in-place summarization anymore; summarization belongs
        # entirely to the compaction mechanism).
        ctx_manager = ContextWindowManager(ContextBudget(model_context_window=_ctx_window))
        trimmed, dropped_messages = ctx_manager.manage_context(session_messages)
        if dropped_messages:
            logger.warning(
                "[workflow] context over budget after pre-turn compaction: dropped %d message(s)",
                len(dropped_messages),
            )
        session_messages = trimmed

        streaming_agent = StreamingAgent(agent, mcp_clients)

        skill_load_ids: set = set()  # track tool_ids that are skill loads
        # tool_id → skill_id; looked up at tool_result time to replace the SSE payload with the curated detail
        skill_id_by_tool_id: Dict[str, str] = {}
        # tool_id → tool_args, used at the tool_result stage to recover view_text_file's file_path/ranges
        view_text_file_args: Dict[str, Dict[str, Any]] = {}
        # enter_plan_mode tool arguments (stashed at the tool_call stage; at
        # the tool_result stage they drive the plan_redirect event + aborting
        # this turn) — isomorphic to batch_plan's human-in-the-loop gate.
        enter_plan_args: Dict[str, Any] = {}

        # Project scope is no longer passed via ContextVar — see the comment at
        # the same spot in _astream_subagent_direct above. All scope-dependent
        # tools captured it by closure when registered in agent_factory; the
        # finishing _persist_artifacts gets the scope explicitly from chats.py.

        try:
            async for event_type, payload in streaming_agent.stream(session_messages, context):
                if event_type == "text_delta":
                    full_response += payload
                    yield {"type": "content", "event": "ai_message", "delta": payload}

                elif event_type == "thinking_delta":
                    yield {"type": "thinking", "delta": payload}

                elif event_type == "tool_call":
                    tool_name = payload.get("name", "unknown")
                    tool_id = payload.get("id", "")
                    tool_args = payload.get("args", {})

                    # In streaming mode, the first chunk for a tool_call may
                    # arrive with empty args.  For view_text_file we need args
                    # to decide if this is a skill load, so skip empty-arg
                    # duplicates until we get the complete args.
                    _is_fast_emit = tool_name in _FAST_EMIT_TOOLS
                    if tool_id and tool_id in displayed_tools:
                        # Fast-emit tools: re-emit when args arrive so the
                        # frontend can update input display immediately.
                        if _is_fast_emit and tool_args:
                            pass  # fall through to emit update
                        else:
                            continue
                    if not _tool_args_ready(tool_name, tool_args):
                        continue
                    if tool_id:
                        displayed_tools.add(tool_id)

                    # Detect skill loading: view_text_file reading a SKILL.md
                    is_skill_load = (
                        tool_name == "view_text_file"
                        and isinstance(tool_args, dict)
                        and "SKILL.md" in str(tool_args.get("file_path", ""))
                    )
                    if is_skill_load and tool_id:
                        skill_load_ids.add(tool_id)
                        _sid = _extract_skill_id_from_path(str(tool_args.get("file_path", "")))
                        if _sid:
                            skill_id_by_tool_id[tool_id] = _sid
                    if (
                        tool_name in ("view_text_file", "Read")
                        and not is_skill_load
                        and tool_id
                        and isinstance(tool_args, dict)
                    ):
                        view_text_file_args[tool_id] = tool_args
                    emit_name = "load_skill" if is_skill_load else tool_name
                    display_name = (
                        "加载技能"
                        if is_skill_load
                        else TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
                    )
                    safe_args = tool_args if isinstance(tool_args, dict) else {}

                    # enter_plan_mode: stash the arguments (in streaming, args
                    # may arrive across frames — take the one carrying
                    # task_description); the tool_result stage uses them to
                    # emit plan_redirect.
                    if tool_name == "enter_plan_mode" and safe_args.get("task_description"):
                        enter_plan_args = safe_args

                    # Resolve sub-agent name for call_subagent tool card
                    _tc_sa_name = ""
                    if tool_name == "call_subagent" and _visible_subagents:
                        _tc_sa_id = safe_args.get("agent_id", "") if safe_args else ""
                        if _tc_sa_id:
                            for _sa in _visible_subagents:
                                if _sa.get("agent_id") == _tc_sa_id:
                                    _tc_sa_name = _sa.get("name", "")
                                    break
                        if not _tc_sa_name and _mentioned_ids:
                            for _sa in _visible_subagents:
                                if _sa.get("agent_id") in _mentioned_ids:
                                    _tc_sa_name = _sa.get("name", "")
                                    break

                    yield {
                        "type": "tool_call",
                        "tool_name": emit_name,
                        "tool_display_name": display_name,
                        "tool_args": safe_args,
                        "input": safe_args,
                        "tool_id": tool_id,
                        **({"subagent_name": _tc_sa_name} if _tc_sa_name else {}),
                    }

                elif event_type == "tool_result":
                    tool_name = payload.get("name", "unknown")
                    tool_id = payload.get("id", "")
                    # Also override tool_name for skill load results
                    is_skill_result = (tool_id and tool_id in skill_load_ids) or (
                        tool_name == "view_text_file"
                        and "SKILL.md" in str(payload.get("content", ""))
                    )
                    if is_skill_result:
                        tool_name = "load_skill"
                    tool_content = payload.get("content", "")

                    # Parse tool result
                    try:
                        tool_result_json = json.loads(tool_content) if tool_content else {}
                    except json.JSONDecodeError:
                        tool_result_json = {"result": tool_content}

                    # Skill load: replace the full SKILL.md text with the same curated detail used by the capability center
                    if is_skill_result:
                        _sid = skill_id_by_tool_id.get(tool_id, "") or _extract_skill_id_from_path(
                            str(tool_content)
                        )
                        tool_result_json = _build_skill_load_payload(_sid)
                    elif tool_name == "view_text_file":
                        # Plain file read (AgentScope built-in view_text_file): replace with file metadata + short preview
                        tool_result_json = _build_view_text_file_payload(
                            view_text_file_args.get(tool_id, {}), tool_content
                        )
                    elif tool_name == "Read":
                        # Claude-Code-style Read tool: JSON payload, content holds the whole file
                        tool_result_json = _build_read_tool_payload(
                            view_text_file_args.get(tool_id, {}), tool_result_json
                        )
                    elif tool_name == "read_artifact":
                        tool_result_json = _build_read_artifact_payload(tool_result_json)

                    # Extract query if present
                    extracted_query = ""
                    if isinstance(tool_result_json, dict) and "result" in tool_result_json:
                        result_data = tool_result_json["result"]
                        if isinstance(result_data, dict):
                            extracted_query = result_data.get(
                                "query", result_data.get("question", "")
                            )

                    # Citations
                    cit_items = extract_citations_with_offset(
                        tool_name, tool_id, tool_result_json, citation_offsets
                    )
                    cit_dicts = [c.to_dict() for c in cit_items]
                    all_citations.extend(cit_dicts)

                    # Resolve sub-agent name from call_subagent result text
                    _tr_sa_name = ""
                    if tool_name == "call_subagent":
                        _res_str = (
                            str(tool_result_json.get("result", ""))
                            if isinstance(tool_result_json, dict)
                            else str(tool_result_json)
                        )
                        if "【" in _res_str and "】" in _res_str:
                            _tr_sa_name = _res_str.split("【", 1)[1].split("】", 1)[0]

                    yield {
                        "type": "tool_result",
                        "tool_name": tool_name,
                        "tool_args": {"query": extracted_query} if extracted_query else {},
                        "result": tool_result_json,
                        "tool_id": tool_id,
                        "citations": cit_dicts,
                        **({"subagent_name": _tr_sa_name} if _tr_sa_name else {}),
                    }

                    # ── enter_plan_mode: switch into plan mode (human-in-the-loop gate, same as batch_plan) ──
                    # The main agent decides the task is complex → calls
                    # enter_plan_mode. Here we emit a plan_redirect event
                    # (carrying task_description) and **abort this turn**: the
                    # frontend uses it to drive the existing plan-mode pipeline
                    # (generate plan → preview card → user confirmation →
                    # execute). The agent does not continue executing on its
                    # own, consistent with the safety semantics of "user
                    # approval required before generating a plan".
                    # The source of truth for tool availability is
                    # agent_factory (not registered for automation/batch/plan
                    # execution etc.); the automation_run check here is purely
                    # a defensive fallback — same as the adjacent batch_plan
                    # block's "mostly defensive". Note that batch_plan's
                    # `_stream_unattended` cannot be reused: it includes
                    # disable_batch_plan (default True for ordinary chats,
                    # which merely lack the batch UI) and would misclassify an
                    # interactive chat as unattended.
                    if (
                        tool_name == "enter_plan_mode"
                        and not context.get("automation_run")
                        and enter_plan_args.get("task_description")
                    ):
                        yield {
                            "type": "plan_redirect",
                            "task_description": str(enter_plan_args.get("task_description", "")),
                        }
                        # Abort the agent loop — the tool description already tells the LLM to hand over control; this enforces it.
                        break

                    # ── Batch execution: pause flow on batch_plan success ──
                    # When the LLM calls the batch_plan MCP tool we treat its
                    # result as a "human-in-the-loop" gate: emit a structured
                    # batch_confirm SSE event with the plan summary, then
                    # terminate the agent loop so the user can review/edit
                    # the prompt template before any item is executed.
                    #
                    # In unattended modes (plan exec / automation) we don't
                    # pause — there's no UI to confirm. We already filter
                    # batch_runner out of those modes' toolkit, so this code
                    # path is mostly defensive (if the agent calls it anyway,
                    # we let it proceed with the plan_id text result).
                    if tool_name == "batch_plan" and not _stream_unattended:
                        bp_data = tool_result_json
                        if (
                            isinstance(bp_data, dict)
                            and "result" in bp_data
                            and isinstance(bp_data["result"], dict)
                        ):
                            bp_data = bp_data["result"]
                        if isinstance(bp_data, dict) and bp_data.get("plan_id"):
                            plan_id = bp_data.get("plan_id")
                            ctx_user_id = str(context.get("user_id", "") or "")
                            ctx_chat_id = str(context.get("chat_id", "") or "")
                            # Backfill user_id + chat_id on the plan: the MCP
                            # subprocess doesn't know who the caller is, so it
                            # creates the plan as 'anonymous'. Patch it now
                            # using the workflow's own context.
                            try:
                                from core.db.engine import SessionLocal
                                from core.db.models import BatchPlan

                                if ctx_user_id:
                                    with SessionLocal() as _db:
                                        _plan = (
                                            _db.query(BatchPlan)
                                            .filter(BatchPlan.plan_id == plan_id)
                                            .first()
                                        )
                                        if _plan and _plan.user_id in ("anonymous", "", None):
                                            _plan.user_id = ctx_user_id
                                            if ctx_chat_id and not _plan.chat_id:
                                                _plan.chat_id = ctx_chat_id
                                            _db.commit()
                                            logger.info(
                                                "[batch] plan %s reassigned to user=%s chat=%s",
                                                plan_id,
                                                ctx_user_id,
                                                ctx_chat_id,
                                            )
                            except Exception as patch_err:
                                logger.warning(
                                    "[batch] failed to backfill plan %s owner: %s",
                                    plan_id,
                                    patch_err,
                                )

                            yield {
                                "type": "batch_confirm",
                                "plan_id": plan_id,
                                "total": bp_data.get("total"),
                                "preview": bp_data.get("preview", []),
                                "default_template": bp_data.get("default_template", ""),
                                "placeholder_keys": bp_data.get("placeholder_keys", []),
                                "source_type": bp_data.get("source_type"),
                                "warnings": bp_data.get("warnings", []),
                                "chat_id": ctx_chat_id if ctx_chat_id else None,
                            }
                            # Stop the agent loop — user must confirm before
                            # any item executes. The MCP tool description
                            # already instructs the LLM not to continue, but
                            # we enforce it here defensively.
                            break

                elif event_type == "heartbeat":
                    yield {"type": "heartbeat"}

                elif event_type == "tool_pending":
                    yield {"type": "tool_pending", **(payload or {})}

                elif event_type == "subagent_event":
                    # Bypass channel for the sub-agent's internal
                    # thinking/tool_call/tool_result/content — attached under
                    # the call_subagent tool card that launched it (linked via
                    # parent_tool_id).
                    yield {"type": "subagent_event", **(payload or {})}

                elif event_type in ("file_confirm", "design_pick"):
                    # Confirmation-type events (§13 MySpace write confirm /
                    # site-design pick-one-of-three): a tool coroutine has
                    # suspended waiting for the user's out-of-band action.
                    # Pass through to the frontend to show the confirmation
                    # card; the agent task stays blocked in that tool and this
                    # SSE stream does not end — after the out-of-band
                    # POST /file-confirm the tool resumes in place.
                    yield {"type": event_type, **(payload or {})}

                elif event_type == "error":
                    # payload may be a real exception object (kind=="err") or a
                    # dict (e.g. ExceedMaxIters mapped to {"kind":..,"name":..}).
                    # Raising the latter directly gives a TypeError, masking
                    # the real situation — wrap it in a RuntimeError.
                    if isinstance(payload, BaseException):
                        raise payload
                    raise RuntimeError(str(payload))

        finally:
            # Same as _astream_subagent_direct: after terminating the stdio
            # subprocesses, (streaming_agent, mcp_clients) must be kept alive
            # permanently to prevent HTTP transport MCP clients blowing up the
            # anyio cancel scope when GC'd. Full story in the comment at the
            # top of the module.
            await streaming_agent.shutdown()
            _persistent_clients.append((streaming_agent, list(mcp_clients)))

    except Exception as e:
        import traceback

        logger.error("stream_workflow_error: %s\n%s", e, traceback.format_exc())
        warnings.append(f"Streaming error: {str(e)[:200]}")

        if displayed_tools and not full_response:
            fallback_msg = (
                "抱歉，我在整理工具调用的结果时遇到了问题。以上是已获取的工具执行结果，请参考。"
            )
            full_response = fallback_msg
            yield {"type": "content", "event": "ai_message", "delta": fallback_msg}
        elif not full_response:
            raise

    yield {
        "type": "meta",
        "route": "main",
        "is_markdown": _looks_markdown(full_response),
        "sources": _resolve_sources_conflict([]),
        "artifacts": [],
        "warnings": warnings,
        "citations": all_citations,
        "usage": streaming_agent.get_usage(),
    }

    # ── [memory] Post-response pipeline (SSE already closed, user isn't waiting) ──
    # No memory is written unless the user opted into memory_write_enabled (first gate).
    # Everything goes through memory_pipeline.schedule_post_response_tasks:
    # global Semaphore + 4 extractors + sanitize + write L1/L2/Session + audit,
    # all executed in the background with bounds.
    if _mem0_write_enabled:
        logger.info(
            "[memory] schedule post-response: full_response_len=%s, user=%s ws=%s",
            len(full_response) if full_response else 0,
            _mem0_user_id,
            _mem0_workspace_id,
        )
        save_memories_background(
            _mem0_user_id,
            user_message,
            full_response,
            _mem0_write_enabled,
            workspace_id=_mem0_workspace_id,
            chat_id=_mem0_chat_id,
            scope_user_id=_mem0_scope_user_id,
        )
    else:
        logger.debug("[workflow] memory save skipped: write_enabled=False (user=%s)", _mem0_user_id)
