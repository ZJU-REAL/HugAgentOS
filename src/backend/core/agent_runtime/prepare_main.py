"""Assemble the main agent and its complete contextual input."""

from __future__ import annotations
from core.agent_runtime.integrations import memory_identity
from core.agent_runtime.integrations import memory_session
from core.llm.runtime import streaming
from core.llm.context import ir_constants
from core.llm.context import items
from core.llm.citations import allocator

import logging
import typing as _typing

import core.agent_runtime.budget as budget
import core.agent_runtime.skills as skills
import core.agent_runtime.targets as targets
import core.capabilities.catalog.resolver as catalog_resolver
import core.llm.factory as factory
from core.agent_runtime.main_state import MainTurn
from core.db.threads import run_in_session

logger = logging.getLogger(__name__)


def _load_subagent_visibility(db, user_id: str, *, evaluation: bool):
    """The user's own sub-agents and the built-in ones they switched off."""
    from core.capabilities.agents.edition import UserAgentService
    from core.auth.users import UserService

    visible = [] if evaluation else UserAgentService(db).list_for_user(user_id)
    return visible, UserService(db).get_disabled_builtin_subagent_ids(user_id)


async def prepare_main(turn: MainTurn):
    import time as _time

    _wf_start = _time.monotonic()

    yield {"type": "thinking", "message": "正在分析您的问题..."}
    for ontology_event in turn.ontology_runtime.get("runtime_events", []):
        yield dict(ontology_event)
    turn.ontology_event_cursor = len(turn.ontology_runtime.get("runtime_events", []))

    _stream_user_id = str(turn.context.get("user_id", ""))
    _stream_model_name = str(turn.context.get("model_name", ""))
    _stream_reranker = bool(turn.context.get("reranker_enabled", False))
    enabled_skill_ids = catalog_resolver.enabled_skill_ids_from_context(turn.context)
    enabled_kb_ids = catalog_resolver.enabled_kb_ids_from_context(turn.context)
    enabled_mcp_ids = catalog_resolver.enabled_mcp_ids_from_context(turn.context)

    # ── Load visible sub-agents for main agent routing ──
    turn.visible_subagents: list = []
    turn.mentioned_ids: list = []
    _explicit_command = turn.context.get("explicit_subagent_command")
    _explicit_agent_id = (
        str(_explicit_command.get("agent_id"))
        if isinstance(_explicit_command, dict) and _explicit_command.get("agent_id")
        else ""
    )
    _mention_agent_id = str(turn.context.get("mention_agent_id") or "")
    try:
        turn.visible_subagents, _disabled_builtin_ids = await run_in_session(
            _load_subagent_visibility,
            _stream_user_id,
            evaluation=bool(turn.context.get("evaluation_session")),
        )
    except Exception as _exc:
        logger.warning("[workflow] failed to load visible subagents: %s", _exc)
        _disabled_builtin_ids = set()

    turn.visible_subagents = targets._runtime_visible_subagents(
        turn.visible_subagents,
        {
            "enabled_skill_ids": enabled_skill_ids or [],
            "enabled_mcp_ids": enabled_mcp_ids or [],
            "enabled_kb_ids": enabled_kb_ids or [],
        },
        _disabled_builtin_ids,
        [_explicit_agent_id, _mention_agent_id],
    )
    # Prefer the structured per-turn target. Text parsing remains a
    # compatibility fallback for callers that only include @name.
    _delegated_agent_id = _explicit_agent_id or _mention_agent_id
    if _delegated_agent_id and any(
        str(item.get("agent_id") or "") == _delegated_agent_id for item in turn.visible_subagents
    ):
        turn.mentioned_ids = [_delegated_agent_id]
    else:
        turn.mentioned_ids = targets._parse_agent_mentions(
            turn.user_message, turn.visible_subagents
        )

    # Create agent (with model-aware CompressionConfig + optional native LTM)
    _plan_chat = bool(turn.context.get("plan_chat", False))
    # 极速模式：配置化检索工具集 + 独立 turbo 提示词 + 迭代硬上限。
    # plan 会话有自己的编排语义，不进入极速裁剪。
    _stream_mode_spec = targets._resolve_mode_spec(turn.context)
    _turbo_chat = _stream_mode_spec is not None and _stream_mode_spec.restricted and not _plan_chat
    if _turbo_chat:
        # 收窄模式下子智能体不再只靠手动呼唤入场：模式自己圈定的那批常驻，
        # 本轮被 @/显式委派命中的那个并上去（系统提示词也就只渲染这几行）。
        # 模式没圈人时退回原契约——只有被呼唤的那个能进。
        turn.visible_subagents = targets._mode_visible_subagents(
            _stream_mode_spec, turn.visible_subagents, turn.mentioned_ids
        )
    turn.agent, turn.mcp_clients = await factory.create_agent_executor(
        agent_spec=None,
        enabled_skill_ids=enabled_skill_ids,
        enabled_mcp_ids=enabled_mcp_ids,
        enabled_kb_ids=enabled_kb_ids,
        current_user_id=_stream_user_id,
        reranker_enabled=_stream_reranker,
        model_name=_stream_model_name,
        model_provider_id=str(turn.context.get("model_provider_id", "") or ""),
        chat_mode=str(turn.context.get("chat_mode", "") or ""),
        turbo_mode=_turbo_chat,
        mode_spec=_stream_mode_spec,
        # 显式呼唤透传：斜杠技能 / 插件展开的 skill_ids 与 mcp_ids。
        # 仅 turbo 模式消费；能力说明仍走 user message 注入，不进全量清单。
        turbo_explicit_skill_ids=(
            skills._explicit_skill_ids_from_context(turn.context) if _turbo_chat else None
        ),
        turbo_explicit_mcp_ids=(
            [m for m in (turn.context.get("mcp_ids") or []) if isinstance(m, str)]
            if _turbo_chat
            else None
        ),
        # 渐进式插件加载：本轮显式呼唤的能力不得被延迟（用户消息注入已承诺
        # 其可用），并作为会话级激活落库。
        invoked_skill_ids=skills._explicit_skill_ids_from_context(turn.context),
        invoked_mcp_ids=[m for m in (turn.context.get("mcp_ids") or []) if isinstance(m, str)],
        required_mcp_ids=(
            [str(turn.context["connector_id"])] if turn.context.get("connector_id") else None
        ),
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
        memory_enabled=turn.memory_enabled,
        approval_mode=targets._approval_mode(turn.context),
        visible_subagents=turn.visible_subagents if turn.visible_subagents else None,
        plan_mode=_plan_chat,
        workflow_mode=bool(turn.context.get("workflow_chat", False)),
        chat_id=turn.context.get("chat_id"),
        sandbox_session_id=turn.context.get("sandbox_session_id"),
        run_id=str(turn.context.get("run_id") or "") or None,
        journal_owner=str(turn.context.get("journal_owner") or "") or None,
        workspace_id=turn.memory_workspace_id,
        project_ctx=targets._extract_project_ctx(turn.context),
        channel_origin=turn.context.get("channel_origin"),
        automation_run=bool(turn.context.get("automation_run")),
        ontology_runtime=turn.context.get("ontology_runtime"),
        # The single positive source of truth for update_plan: enabled
        # only for "interactive main chats that can host plan mode" — has
        # a chat_id, not a channel bot, not automation, not plan_chat, not
        # batch. Channels/automation have no plan-bar UI; plan_chat
        # have their own orchestration and must not nest plan tracking.
        top_level_chat=(
            bool(turn.context.get("chat_id"))
            and not turn.context.get("channel_origin")
            and not bool(turn.context.get("automation_run"))
            and not _plan_chat
        ),
    )

    logger.info("[workflow] agent created in %.0fms", (_time.monotonic() - _wf_start) * 1000)

    # 证据锚点：把发号器绑到 agent 上（中间件与本函数由此共享同一个计数器；
    # 仅靠 ContextVar 不行——本函数是 async generator，与 agent 执行所在的
    # task 上下文不互通）
    allocator.attach_allocator(turn.agent, turn.anchor_allocator)

    # ── Inject the per-turn sub-agent constraint into the current user message ──
    # Keeps it OUT of the system prompt so the LLM provider's prefix cache
    # hits across turns within a chat (otherwise every turn with different
    # targets would re-build the cache from scratch). A strict natural-
    # language command remains on this normal model stream; it only
    # constrains the model's next real tool call instead of fabricating one
    # in the workflow layer.
    if turn.mentioned_ids and turn.visible_subagents:
        from core.llm.subagents.tool import build_explicit_subagent_command_hint, build_subagent_mention_hint

        if _explicit_agent_id or _mention_agent_id:
            _mention_hint = build_explicit_subagent_command_hint(
                turn.visible_subagents,
                _explicit_agent_id or _mention_agent_id,
            )
        else:
            _mention_hint = build_subagent_mention_hint(
                turn.visible_subagents,
                turn.mentioned_ids,
            )
        if (
            _mention_hint
            and turn.session_messages
            and turn.session_messages[-1].get("role") in ("user", "human")
        ):
            mention_item = items.make_text_context_item(
                _mention_hint,
                item_id="harness:subagent_routing",
                kind=ir_constants.KIND_REMINDER,
                origin="harness:subagent_routing",
                trust="system",
                created_seq=0,
                priority=950,
                token_budget=4_000,
                truncation_policy=ir_constants.POLICY_NEVER,
            )
            turn.session_messages.insert(
                -1,
                {
                    "role": "user",
                    "content": _mention_hint,
                    ir_constants.SESSION_CONTEXT_META_KEY: items.session_context_metadata(
                        mention_item
                    ),
                },
            )

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
    # Pre-turn compaction and the request assembler share this value.
    _actual_model, turn.ctx_window = budget._resolve_agent_model_runtime(
        turn.agent, _stream_model_name
    )
    try:
        from core.chat.compaction.service import maybe_run_pre_turn_compaction

        turn.session_messages, _ = await maybe_run_pre_turn_compaction(
            turn.context.get("chat_id"),
            turn.session_messages,
            run_id=str(turn.context.get("run_id") or ""),
            **budget._pre_turn_compaction_inputs(turn.agent, turn.ctx_window, _actual_model),
        )
    except Exception as _pt_exc:  # noqa: BLE001
        from core.harness.hooks import HookPaused

        if isinstance(_pt_exc, HookPaused):
            raise
        logger.warning("[workflow] pre-turn compaction failed: %s", _pt_exc)

    # ── Session-constant blocks: user identity + the chat's frozen memory
    # snapshot. Built on the first turn (L1 profile always, L2 facts only if
    # retrieval beat its budget), replayed byte-for-byte on every turn after,
    # so this head of the message list never moves the prefix-cache boundary.
    turn.session_messages = await memory_session.inject_session_blocks(
        turn.session_messages,
        identity_block=(
            ""
            if turn.context.get("evaluation_session")
            else await memory_identity.build_user_identity_block(turn.memory_user_id)
        ),
        memory_block=await memory_session.resolve_session_memory(turn.session_memory),
    )

    # The final request assembler receives the full post-compaction
    # candidate set.  It is the only budget selector, and records every
    # included, truncated, or excluded item in the context manifest.

    turn.streaming_agent = streaming.StreamingAgent(turn.agent, turn.mcp_clients)

    turn.skill_load_ids: set = set()  # track tool_ids that are skill loads
    # tool_id → skill_id; looked up at tool_result time to replace the SSE payload with the curated detail
    turn.skill_id_by_tool_id: _typing.Dict[str, str] = {}
    # tool_id → tool_args, used at the tool_result stage to recover view_text_file's file_path/ranges
    turn.view_text_file_args: _typing.Dict[str, _typing.Dict[str, _typing.Any]] = {}
