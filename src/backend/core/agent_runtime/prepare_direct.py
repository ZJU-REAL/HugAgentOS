"""Assemble a directly selected agent and its contextual input."""

from __future__ import annotations
from core.agent_runtime.integrations import memory_identity
from core.agent_runtime.integrations import memory_session
from core.llm.runtime import streaming
from core.llm.citations import allocator

import logging
import typing as _typing

import core.agent_runtime.budget as budget
import core.agent_runtime.targets as targets
import core.llm.factory as factory
from core.agent_runtime.direct_state import DirectTurn

import time as _time

logger = logging.getLogger(__name__)


async def prepare_direct(turn: DirectTurn):
    yield {"type": "thinking", "message": "正在连接子智能体..."}
    for ontology_event in turn.ontology_runtime.get("runtime_events", []):
        yield dict(ontology_event)
    turn.ontology_event_cursor = len(turn.ontology_runtime.get("runtime_events", []))

    _stream_user_id = str(turn.context.get("user_id", ""))
    _stream_model_name = str(turn.context.get("model_name", ""))
    _stream_model_provider_id = str(turn.context.get("model_provider_id", "") or "")
    _stream_chat_mode = str(turn.context.get("chat_mode", "") or "")
    _stream_reranker = bool(turn.context.get("reranker_enabled", False))

    # Create agent with sub-agent config overrides
    turn.agent, turn.mcp_clients = await factory.create_agent_executor(
        agent_spec=None,
        enabled_mcp_ids=None,  # overridden by user_agent inside factory
        enabled_skill_ids=None,  # overridden by user_agent inside factory
        enabled_kb_ids=None,  # overridden by user_agent inside factory
        current_user_id=_stream_user_id,
        reranker_enabled=_stream_reranker,
        model_name=_stream_model_name,
        model_provider_id=_stream_model_provider_id,
        chat_mode=_stream_chat_mode,
        memory_enabled=turn.memory_enabled,
        agent_api_scope=turn.context.get("agent_api_scope"),
        user_agent=turn.user_agent,
        approval_mode=targets._approval_mode(turn.context),
        required_skill_id=str(turn.context.get("skill_id") or "") or None,
        required_skill_name=str(turn.context.get("skill_name") or "") or None,
        required_plugin_id=str(turn.context.get("plugin_id") or "") or None,
        required_plugin_name=str(turn.context.get("plugin_name") or "") or None,
        required_plugin_skill_ids=[
            str(item)
            for item in (turn.context.get("plugin_skill_ids") or [])
            if isinstance(item, str) and item
        ],
        required_plugin_mcp_ids=[
            str(item)
            for item in (turn.context.get("plugin_mcp_ids") or [])
            if isinstance(item, str) and item
        ],
        # Same as the main agent: pass chat_id → the sandbox session uses
        # the chat_id-keyed "user-bound persistent sandbox" (mounting the
        # per-user credential volumes for lark/dws/email etc.). Omitting it
        # falls back to an ephemeral light sandbox (no credentials) — the
        # root cause of Feishu/DingTalk CLIs reporting "not configured" in
        # direct sub-agent conversations.
        chat_id=turn.context.get("chat_id"),
        sandbox_session_id=turn.context.get("sandbox_session_id"),
        run_id=str(turn.context.get("run_id") or "") or None,
        journal_owner=str(turn.context.get("journal_owner") or "") or None,
        workspace_id=turn.memory_workspace_id,
        project_ctx=targets._extract_project_ctx(turn.context),
        channel_origin=turn.context.get("channel_origin"),
        automation_run=bool(turn.context.get("automation_run")),
        ontology_runtime=turn.context.get("ontology_runtime"),
    )

    logger.info("[subagent] agent created in %.0fms", (_time.monotonic() - turn.wf_start) * 1000)
    _, turn.ctx_window = budget._resolve_agent_model_runtime(turn.agent, _stream_model_name)

    # 证据锚点：把发号器绑到 agent 上（中间件与本函数由此共享同一个计数器；
    # 仅靠 ContextVar 不行——本函数是 async generator，与 agent 执行所在的
    # task 上下文不互通）
    allocator.attach_allocator(turn.agent, turn.anchor_allocator)

    # ── Session-constant blocks: user identity + the chat's frozen memory
    # snapshot (built on the first turn, replayed byte-for-byte after). ──
    turn.session_messages = await memory_session.inject_session_blocks(
        turn.session_messages,
        identity_block=(
            ""
            if turn.context.get("agent_api_scope") or turn.context.get("evaluation_session")
            else await memory_identity.build_user_identity_block(turn.memory_user_id)
        ),
        memory_block=await memory_session.resolve_session_memory(turn.session_memory),
    )

    # The canonical ContextAssembler owns selection and truncation.  Feed
    # it the complete candidate set so every exclusion is visible in the
    # request manifest rather than silently pre-trimming shared history.
    turn.streaming_agent = streaming.StreamingAgent(turn.agent, turn.mcp_clients)
    turn.skill_load_ids: set = set()
    # tool_id → skill_id (parsed from the tool_call's file_path; looked up at tool_result time to replace the SSE payload with the curated detail, avoiding sending the full SKILL.md text to the frontend)
    turn.skill_id_by_tool_id: _typing.Dict[str, str] = {}
    # tool_id → tool_args, used at the tool_result stage to recover view_text_file's file_path/ranges
    turn.view_text_file_args: _typing.Dict[str, _typing.Dict[str, _typing.Any]] = {}

    # Project scope is no longer passed via ContextVar — it now travels as
    # explicit parameters along the call chain (agent_factory closes
    # ProjectScope into every register_* tool; chats.py's finishing
    # _persist_artifacts reconstructs the scope from the workflow context
    # and passes it explicitly). See the header comment in
    # core/projects/scope.py.

    # Never keep a ContextVar token alive across an async-generator
    # ``yield``. The consumer may resume this generator in a copied
    # context, making reset(token) fail with "created in a different
    # Context". Enter the audit scope only while advancing the underlying
    # agent stream; tool execution and persistence happen during anext().
    turn.direct_stream = turn.streaming_agent.stream(
        turn.session_messages, turn.context
    ).__aiter__()
