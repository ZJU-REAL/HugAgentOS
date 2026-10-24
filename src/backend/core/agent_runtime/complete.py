"""core agent_runtime complete responsibilities."""

from __future__ import annotations
from core.ontology import output_review_state as output_review_state_module
from core.ontology import runtime_pack as runtime_pack_module
from core.llm.context import rendering as rendering_module
from core.llm.context import ir_state as ir_state_module
from core.llm.context import items as items_module
from core.ontology import runtime_policy

import dataclasses as _dataclasses
import logging
import typing as _typing
from dataclasses import dataclass
from typing import Any, Dict, List

import core.agent_runtime.budget as budget
import core.agent_runtime.integrations.ontology_evidence as ontology_evidence
import core.agent_runtime.integrations.ontology_repair as ontology_repair
import core.agent_runtime.skills as skills
import core.agent_runtime.targets as targets
import core.chat.message_parser as message_parser
import core.capabilities.catalog.resolver as catalog_resolver
import core.llm.context.adapter as context_adapter
import core.llm.context.ir as context_ir
import core.llm.factory as factory
import core.llm.mcp.manager as mcp_manager
import core.llm.runtime.message_compat as message_compat
import core.ontology.validator as validator
import core.ontology.service as ontology_service

logger = logging.getLogger(__name__)


@dataclass
class WorkflowResult:
    route: str = "main"
    response: str = ""
    is_markdown: bool = False
    sources: _typing.List[_typing.Dict[str, _typing.Any]] = _dataclasses.field(default_factory=list)
    artifacts: _typing.List[_typing.Dict[str, _typing.Any]] = _dataclasses.field(
        default_factory=list
    )
    warnings: _typing.List[str] = _dataclasses.field(default_factory=list)
    meta: _typing.Dict[str, _typing.Any] = _dataclasses.field(default_factory=dict)


def run_chat_workflow(
    *,
    session_messages: List[Dict[str, Any]],
    user_message: str,
    context: Dict[str, Any],
) -> WorkflowResult:
    """Run route -> target execution."""
    from core.harness.registry.evaluation import apply_evaluation_scope

    context = apply_evaluation_scope(context)

    _explicit_command = context.get("explicit_subagent_command")
    _explicit_agent_id = (
        str(_explicit_command.get("agent_id"))
        if isinstance(_explicit_command, dict) and _explicit_command.get("agent_id")
        else None
    )
    _mention_agent_id = str(context.get("mention_agent_id") or "") or None
    _direct_agent_id = targets._direct_agent_id_from_context(context)
    _direct_user_agent = None
    _direct_read_only = False
    _direct_allow_bash = True
    _visible_subagents: _typing.List[_typing.Dict[str, _typing.Any]] = []
    _request_ontology_runtime = context.get("ontology_runtime")
    if not isinstance(_request_ontology_runtime, dict):
        _request_ontology_runtime = {}
        context["ontology_runtime"] = _request_ontology_runtime
    if _direct_agent_id:
        _direct_read_only, _direct_allow_bash = targets._direct_agent_execution_permissions(
            _direct_agent_id
        )
        _direct_user_agent = targets._load_direct_user_agent(
            _direct_agent_id,
            str(context.get("user_id", "")),
            {
                "enabled_skill_ids": catalog_resolver.enabled_skill_ids_from_context(context) or [],
                "enabled_mcp_ids": catalog_resolver.enabled_mcp_ids_from_context(context) or [],
                "enabled_kb_ids": catalog_resolver.enabled_kb_ids_from_context(context) or [],
                "sandbox_tools_enabled": bool(context.get("sandbox_tools_enabled", False)),
                "code_capability_enabled": bool(context.get("code_capability_enabled", False)),
            },
        )
        activations = runtime_pack_module.activate_runtime_for_asset(
            _request_ontology_runtime,
            kind="subagent",
            asset_id=_direct_agent_id,
            tags=list((_direct_user_agent.extra_config or {}).get("ontology_tags") or []),
        )
        ontology_evidence._record_ontology_activations(
            activations,
            _request_ontology_runtime,
            context,
        )
    else:
        try:
            from core.db.engine import SessionLocal as _SessionLocal
            from core.capabilities.agents.edition import UserAgentService as _UAS
            from core.auth.users import UserService as _UserService

            with _SessionLocal() as _db:
                _visible_subagents = (
                    []
                    if context.get("evaluation_session")
                    else _UAS(_db).list_for_user(str(context.get("user_id", "")))
                )
                _disabled_builtin_ids = _UserService(_db).get_disabled_builtin_subagent_ids(
                    str(context.get("user_id", ""))
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[workflow] failed to load visible subagents: %s", exc)
            _disabled_builtin_ids = set()
    for skill_id in skills._explicit_skill_ids_from_context(context):
        runtime_policy.resolve_runtime_asset_tags(
            runtime=_request_ontology_runtime,
            kind="skill",
            asset_id=skill_id,
            user_id=str(context.get("user_id", "") or ""),
        )
        activations = runtime_pack_module.activate_runtime_for_asset(
            _request_ontology_runtime,
            kind="skill",
            asset_id=skill_id,
        )
        ontology_evidence._record_ontology_activations(
            activations,
            _request_ontology_runtime,
            context,
        )

    # ── Inject explicit skill instructions ──
    skill_msg = skills._build_skill_injection(context)
    if skill_msg:
        session_messages.insert(-1, skill_msg)
        logger.info(
            "[skill_inject] injected skill instructions for '%s'",
            context.get("skill_id"),
        )

    warnings: _typing.List[str] = []
    enabled_skill_ids = (
        None if _direct_user_agent else catalog_resolver.enabled_skill_ids_from_context(context)
    )
    enabled_kb_ids = (
        None if _direct_user_agent else catalog_resolver.enabled_kb_ids_from_context(context)
    )
    enabled_mcp_ids = (
        None if _direct_user_agent else catalog_resolver.enabled_mcp_ids_from_context(context)
    )

    _workflow_user_id = str(context.get("user_id", ""))
    _workflow_model_name = str(context.get("model_name", ""))
    _workflow_model_provider_id = str(context.get("model_provider_id", "") or "")
    _workflow_chat_mode = str(context.get("chat_mode", "") or "")
    _workflow_mem_enabled = bool(context.get("memory_enabled", False))
    _reranker_enabled = bool(context.get("reranker_enabled", False))

    if _direct_user_agent is None:
        _visible_subagents = targets._runtime_visible_subagents(
            _visible_subagents,
            {
                "enabled_skill_ids": enabled_skill_ids or [],
                "enabled_mcp_ids": enabled_mcp_ids or [],
                "enabled_kb_ids": enabled_kb_ids or [],
            },
            _disabled_builtin_ids,
            [_explicit_agent_id, _mention_agent_id],
        )

    _workflow_mode_spec = targets._resolve_mode_spec(context)
    _workflow_turbo = (
        _direct_user_agent is None
        and _workflow_mode_spec is not None
        and _workflow_mode_spec.restricted
    )
    if _workflow_turbo:
        # 收窄模式：可见子智能体 = 模式圈定的那批 ∪ 本轮被显式委派/@ 的那个
        # （此路径无文本 @ 解析）。
        _turbo_delegated = str(_explicit_agent_id or _mention_agent_id or "")
        _visible_subagents = targets._mode_visible_subagents(
            _workflow_mode_spec, _visible_subagents, [_turbo_delegated]
        )

    async def _run():
        agent, mcp_clients = await factory.create_agent_executor(
            agent_spec=None,
            enabled_skill_ids=enabled_skill_ids,
            enabled_mcp_ids=enabled_mcp_ids,
            enabled_kb_ids=enabled_kb_ids,
            current_user_id=_workflow_user_id,
            reranker_enabled=_reranker_enabled,
            model_name=_workflow_model_name,
            model_provider_id=_workflow_model_provider_id,
            chat_mode=_workflow_chat_mode,
            turbo_mode=_workflow_turbo,
            mode_spec=_workflow_mode_spec,
            turbo_explicit_skill_ids=(
                skills._explicit_skill_ids_from_context(context) if _workflow_turbo else None
            ),
            turbo_explicit_mcp_ids=(
                [m for m in (context.get("mcp_ids") or []) if isinstance(m, str)]
                if _workflow_turbo
                else None
            ),
            # 渐进式插件加载：本轮显式呼唤的能力不得被延迟（用户消息注入已承诺
            # 其可用），并作为会话级激活落库。
            invoked_skill_ids=skills._explicit_skill_ids_from_context(context),
            invoked_mcp_ids=[m for m in (context.get("mcp_ids") or []) if isinstance(m, str)],
            required_mcp_ids=(
                [str(context["connector_id"])] if context.get("connector_id") else None
            ),
            required_skill_id=str(context.get("skill_id") or "") or None,
            required_skill_name=str(context.get("skill_name") or "") or None,
            required_plugin_id=str(context.get("plugin_id") or "") or None,
            required_plugin_name=str(context.get("plugin_name") or "") or None,
            required_plugin_skill_ids=[
                str(item)
                for item in (context.get("plugin_skill_ids") or [])
                if isinstance(item, str) and item
            ],
            required_plugin_mcp_ids=[
                str(item)
                for item in (context.get("plugin_mcp_ids") or [])
                if isinstance(item, str) and item
            ],
            memory_enabled=_workflow_mem_enabled,
            workflow_mode=bool(context.get("workflow_chat", False)),
            user_agent=_direct_user_agent,
            read_only=_direct_read_only,
            allow_bash=_direct_allow_bash,
            approval_mode=targets._approval_mode(context),
            visible_subagents=_visible_subagents if _visible_subagents else None,
            chat_id=context.get("chat_id"),
            sandbox_session_id=context.get("sandbox_session_id"),
            run_id=str(context.get("run_id") or "") or None,
            journal_owner=str(context.get("journal_owner") or "") or None,
            workspace_id=str(context.get("workspace_id") or "default"),
            project_ctx=targets._extract_project_ctx(context),
            channel_origin=context.get("channel_origin"),
            automation_run=bool(context.get("automation_run")),
            ontology_runtime=context.get("ontology_runtime"),
        )
        try:
            # AgentScope 2.0: ctx → agent.state (AgentRuntimeState), replacing _jx_context.
            # Uploaded/history files are written into state by
            # apply_request_context, then injected uniformly at reply time by
            # FileContextMiddleware (not hand-copied here).
            try:
                agent.state.apply_request_context(context, user_message or "")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[workflow] set agent.state failed: %s", exc)

            _actual_model, _ctx_window = budget._resolve_agent_model_runtime(
                agent, _workflow_model_name
            )

            # PreTurn compaction safety net (symmetric with the streaming path
            # — both workflow entry points protect themselves, and future new
            # callers get it automatically). Zero overhead below the threshold.
            _pt_messages = session_messages
            try:
                from core.chat.compaction.service import maybe_run_pre_turn_compaction

                _pt_messages, _ = await maybe_run_pre_turn_compaction(
                    context.get("chat_id"),
                    session_messages,
                    run_id=str(context.get("run_id") or ""),
                    **budget._pre_turn_compaction_inputs(agent, _ctx_window, _actual_model),
                )
            except Exception as _pt_exc:  # noqa: BLE001
                from core.harness.hooks import HookPaused

                if isinstance(_pt_exc, HookPaused):
                    raise
                logger.warning("[workflow] pre-turn compaction failed: %s", _pt_exc)

            # Load history EXCLUDING the last user message — reply() adds it.
            history = list(_pt_messages)
            if history and history[-1].get("role") in ("user", "human"):
                history.pop()

            if history:
                agent.state.context.extend(message_compat.session_to_msgs(history))

            # Uploaded-file context is injected uniformly at reply time by
            # FileContextMiddleware (apply_request_context already wrote
            # uploaded_files into state; the middleware validates attachment
            # ownership by state.user_id); no manual append here, otherwise it
            # would duplicate the middleware's injection and waste tokens.

            model_user_message = user_message or ""
            _delegated_agent_id = _explicit_agent_id or _mention_agent_id
            if _delegated_agent_id and _visible_subagents:
                from core.llm.subagents.tool import build_explicit_subagent_command_hint

                explicit_hint = build_explicit_subagent_command_hint(
                    _visible_subagents,
                    _delegated_agent_id,
                )
                if explicit_hint:
                    rendering_module.append_context_text(
                        agent,
                        explicit_hint,
                        kind=ir_state_module.KIND_REMINDER,
                        origin="harness:subagent_routing",
                        trust="system",
                        priority=950,
                        token_budget=4_000,
                        truncation_policy=ir_state_module.POLICY_NEVER,
                    )
            request_seq = rendering_module.next_request_sequence(agent.state.context)
            user_msg = rendering_module.render_context_item(
                items_module.make_text_context_item(
                    model_user_message,
                    item_id=f"request:user_input:{request_seq}",
                    kind=ir_state_module.KIND_USER_INPUT,
                    origin="user:chat",
                    trust="user",
                    created_seq=request_seq,
                    priority=1_000,
                    token_budget=100_000,
                    truncation_policy=ir_state_module.POLICY_NEVER,
                )
            )
            result = await agent.reply(inputs=user_msg)
            response = result.get_text_content() or ""
            ontology_runtime = context.get("ontology_runtime")
            if not isinstance(ontology_runtime, dict):
                ontology_runtime = {}
                context["ontology_runtime"] = ontology_runtime
            review_owner = ontology_evidence._ontology_review_owner(ontology_runtime, context)
            review_claimed = bool(
                response and output_review_state_module.claim_output_review(ontology_runtime, owner=review_owner)
            )
            if review_claimed:
                from core.ontology.reviewer import review_ontology_output

                async def _remediate(payload: Dict[str, Any]) -> str:
                    repair_seq = rendering_module.next_request_sequence(agent.state.context)
                    repair_msg = rendering_module.render_context_item(
                        items_module.make_text_context_item(
                            ontology_repair._ontology_repair_prompt(payload),
                            item_id=f"request:ontology_repair:{repair_seq}",
                            kind=ir_state_module.KIND_REMINDER,
                            origin="harness:ontology_repair",
                            trust="system",
                            created_seq=repair_seq,
                            priority=900,
                            token_budget=4_000,
                            truncation_policy=ir_state_module.POLICY_HEAD_TAIL,
                        )
                    )
                    repaired_result = await agent.reply(inputs=repair_msg)
                    return (repaired_result.get_text_content() or "").strip()

                try:
                    review = await review_ontology_output(
                        task=user_message,
                        answer=response,
                        runtime=ontology_runtime,
                        trace=[],
                        citations=[],
                        user_id=_workflow_user_id,
                        chat_id=context.get("chat_id"),
                        model_name=_workflow_model_name or None,
                        model_provider_id=_workflow_model_provider_id or None,
                        trace_complete=False,
                        remediate=_remediate,
                    )
                except BaseException:
                    output_review_state_module.release_output_review(ontology_runtime, owner=review_owner)
                    raise
                output_review_state_module.complete_output_review(
                    ontology_runtime,
                    owner=review_owner,
                    verdict=str(review.get("verdict") or "unknown"),
                    attempts=int(review.get("attempts") or 1),
                )
                ontology_runtime.setdefault("output_review", {}).update(
                    {
                        "revised": bool(review.get("revised")),
                        "annotated": bool(review.get("annotated")),
                        "repair_attempts": int(review.get("repair_attempts") or 0),
                        "violations": review.get("violations") or [],
                        "affected_claims": review.get("affected_claims") or [],
                        "latency_ms": review.get("latency_ms"),
                    }
                )
                response = review["answer"]
            return response
        finally:
            await mcp_manager.close_clients(mcp_clients)

    try:
        import asyncio as _asyncio

        response = _asyncio.run(_run())
    except Exception as e:
        warnings.append(f"Agent execution error: {str(e)[:200]}")
        response = ""

    return WorkflowResult(
        route=f"subagent:{_direct_agent_id}" if _direct_agent_id else "main",
        response=response,
        is_markdown=message_parser.looks_markdown(response),
        sources=message_parser.resolve_sources_conflict([]),
        artifacts=[],
        warnings=warnings,
        meta={
            "ontology_governance": ontology_evidence._ontology_governance_summary(
                context.get("ontology_runtime")
                if isinstance(context.get("ontology_runtime"), dict)
                else {}
            )
        },
    )
